package kvm

import (
	"errors"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/jetkvm/kvm/internal/cuaapi"
	"github.com/jetkvm/kvm/internal/native"
)

// CUA (computer-using agent) API: a small token-protected HTTP surface under
// /api/cua that lets the off-device cua/ service read the screen and drive the
// HID gadget without a WebRTC session, so a human can keep watching in the
// browser. The logic lives in internal/cuaapi; this file wires it to the
// device.

// cuaLeaseIdle is how long the pipeline stays up for the CUA after its last
// frame request when no browser session is connected.
const cuaLeaseIdle = 60 * time.Second

var (
	cuaFrames = cuaapi.NewFrameBuffer()
	// Created in init: its callbacks reach code that reads the lease itself
	// (display advertisement), which a package-level initializer cannot.
	cuaVideoLease *cuaapi.Lease
)

func init() {
	cuaVideoLease = cuaapi.NewLease(cuaLeaseIdle, cuaAcquireVideo, cuaReleaseVideo)
}

// cuaAcquireVideo starts the pipeline for the CUA when no session runs it.
// A pause only because the browser tab is hidden does not stop the CUA; a pause
// for virtual media (memory pressure) does.
func cuaAcquireVideo() error {
	withSession := getActiveSessions() > 0
	if !withSession {
		stopVideoSleepModeTicker()
		_ = setHostDisplayAdvertised(true, "cua_lease", false)
	}

	videoPauseMu.Lock()
	defer videoPauseMu.Unlock()
	if videoPausedBy != nil && !videoPauseHidden {
		return errors.New("video stream is paused by a browser session (virtual media)")
	}
	if nativeVideoCodec >= 0 {
		return nil
	}
	codec := int(cuaapi.CodecH264)
	if withSession && currentSession != nil {
		codec = sessionVideoCodecType(currentSession)
	}
	startNativeVideoWithCodecLocked(codec)
	return nil
}

// cuaReleaseVideo mirrors onLastSessionDisconnected for the CUA lease.
func cuaReleaseVideo() {
	if getActiveSessions() > 0 {
		// A tab that went to the background during the run asked for a pause;
		// honor it now.
		videoPauseMu.Lock()
		defer videoPauseMu.Unlock()
		if videoPausedBy != nil && nativeVideoCodec >= 0 {
			cuaLogger.Info().Msg("CUA lease expired, applying the browser's video pause")
			_ = stopNativeVideoLocked()
		}
		return
	}
	cuaLogger.Info().Msg("CUA lease expired, stopping video stream")
	stopNativeVideo()
	_ = applyHostDisplayAdvertisement("cua_lease_released")
	startVideoSleepModeTicker()
}

// cuaBackend implements cuaapi.Backend against the device.
type cuaBackend struct{}

func (cuaBackend) VideoInfo() cuaapi.VideoInfo {
	s := lastVideoState
	return cuaapi.VideoInfo{
		Ready:     s.Ready,
		Streaming: s.Streaming != native.VideoStreamingStatusInactive,
		Error:     s.Error,
		Width:     s.Width,
		Height:    s.Height,
		FPS:       s.FramePerSecond,
	}
}

func (cuaBackend) EnsureVideo() error {
	if err := cuaVideoLease.Touch(); err != nil {
		return err
	}
	// The lease may be active while the pipeline was stopped underneath it
	// (e.g. a pause/resume cycle); restart it in that case.
	videoPauseMu.Lock()
	running := nativeVideoCodec >= 0
	videoPauseMu.Unlock()
	if !running {
		return cuaAcquireVideo()
	}
	return nil
}

func (cuaBackend) AbsMouse(x, y int, buttons uint8) error { return rpcAbsMouseReport(x, y, buttons) }
func (cuaBackend) Wheel(dy, dx int8) error                { return rpcWheelReport(dy, dx) }
func (cuaBackend) Keyboard(modifier byte, keys []byte) error {
	return rpcKeyboardReport(modifier, keys)
}
func (cuaBackend) ActiveSessions() int { return getActiveSessions() }

func cuaAuthConfig() (bool, string) {
	return config.CuaApiEnabled, config.CuaApiToken
}

func registerCuaRoutes(r *gin.Engine) {
	cuaapi.NewServer(cuaBackend{}, cuaFrames, cuaAuthConfig).Register(r.Group("/api/cua"))
}

// --- JSON-RPC (used from the web UI / developer console to turn it on) ---

type CuaApiConfig struct {
	Enabled bool   `json:"enabled"`
	Token   string `json:"token"`
}

func rpcGetCuaApiConfig() (CuaApiConfig, error) {
	ensureConfigLoaded()
	return CuaApiConfig{Enabled: config.CuaApiEnabled, Token: config.CuaApiToken}, nil
}

// rpcSetCuaApiConfig enables or disables the API. A token is generated on
// first enable, and regenerateToken forces a new one (revoking the old).
func rpcSetCuaApiConfig(enabled bool, regenerateToken bool) (CuaApiConfig, error) {
	ensureConfigLoaded()
	prevEnabled, prevToken := config.CuaApiEnabled, config.CuaApiToken
	config.CuaApiEnabled = enabled
	if regenerateToken || (enabled && config.CuaApiToken == "") {
		config.CuaApiToken = uuid.New().String()
	}
	if err := SaveConfig(); err != nil {
		config.CuaApiEnabled, config.CuaApiToken = prevEnabled, prevToken
		return CuaApiConfig{}, err
	}
	if !enabled {
		cuaVideoLease.Release()
	}
	return CuaApiConfig{Enabled: config.CuaApiEnabled, Token: config.CuaApiToken}, nil
}
