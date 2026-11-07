package cuaapi

import (
	"context"
	"crypto/subtle"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

// AbsMax is the upper bound of the absolute pointer coordinates (both axes).
const AbsMax = 32767

// Limits that keep one request from monopolising the HID gadget.
const (
	maxEvents     = 1000
	maxEventDelay = 2000 // ms
	frameTimeout  = 3 * time.Second
	hidKeyBufSize = 6
)

// VideoInfo is what /state reports about the captured HDMI signal.
type VideoInfo struct {
	Ready     bool    `json:"ready"`
	Streaming bool    `json:"streaming"`
	Error     string  `json:"error,omitempty"`
	Width     int     `json:"width"`
	Height    int     `json:"height"`
	FPS       float64 `json:"fps"`
}

// Backend is implemented by the root package against the real device.
type Backend interface {
	VideoInfo() VideoInfo
	// EnsureVideo makes sure the encoder runs (and extends the CUA lease).
	EnsureVideo() error
	AbsMouse(x, y int, buttons uint8) error
	Wheel(dy, dx int8) error
	Keyboard(modifier byte, keys []byte) error
	// ActiveSessions is the number of WebRTC sessions (human viewers).
	ActiveSessions() int
}

// AuthConfig returns whether the API is enabled and the bearer token.
type AuthConfig func() (enabled bool, token string)

// Server holds the HTTP handlers. Input requests are serialised so mouse paths
// and key sequences from concurrent calls do not interleave.
type Server struct {
	backend Backend
	frames  *FrameBuffer
	auth    AuthConfig
	inputMu sync.Mutex
	// sleep is replaceable in tests.
	sleep func(ctx context.Context, d time.Duration) error
}

func NewServer(b Backend, frames *FrameBuffer, auth AuthConfig) *Server {
	return &Server{backend: b, frames: frames, auth: auth, sleep: ctxSleep}
}

// Register mounts the API under the given group (normally /api/cua).
func (s *Server) Register(g *gin.RouterGroup) {
	g.Use(s.authMiddleware())
	g.GET("/state", s.handleState)
	g.GET("/frame", s.handleFrame)
	g.POST("/mouse", s.handleMouse)
	g.POST("/wheel", s.handleWheel)
	g.POST("/keys", s.handleKeys)
}

func (s *Server) authMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		enabled, token := s.auth()
		if !enabled || token == "" {
			c.AbortWithStatusJSON(http.StatusNotFound, gin.H{"error": "CUA API is disabled"})
			return
		}
		got, ok := strings.CutPrefix(c.GetHeader("Authorization"), "Bearer ")
		if !ok || subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid or missing bearer token"})
			return
		}
		c.Next()
	}
}

func (s *Server) handleState(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"video":          s.backend.VideoInfo(),
		"codec":          s.frames.Codec().String(),
		"activeSessions": s.backend.ActiveSessions(),
		"absMax":         AbsMax,
	})
}

// handleFrame returns the GOP since the last keyframe as raw Annex-B bytes.
// ?after=<seq> waits for a frame newer than seq (use the X-Frame-Seq of a
// previous response to make sure the picture was captured after an action).
func (s *Server) handleFrame(c *gin.Context) {
	if err := s.backend.EnsureVideo(); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "video unavailable: " + err.Error()})
		return
	}
	var after uint64
	if v := c.Query("after"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "after must be an unsigned integer"})
			return
		}
		after = n
	}
	snap, ok := s.frames.WaitSnapshot(after, frameTimeout)
	if !ok {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "no decodable frame yet (is an HDMI source connected?)"})
		return
	}
	info := s.backend.VideoInfo()
	h := c.Writer.Header()
	h.Set("X-Codec", snap.Codec.String())
	h.Set("X-Frame-Seq", strconv.FormatUint(snap.Seq, 10))
	h.Set("X-Frame-Count", strconv.Itoa(snap.Frames))
	h.Set("X-Width", strconv.Itoa(info.Width))
	h.Set("X-Height", strconv.Itoa(info.Height))
	h.Set("X-Frame-Age-Ms", strconv.FormatInt(time.Since(snap.LastFrame).Milliseconds(), 10))
	h.Set("Cache-Control", "no-store")
	c.Data(http.StatusOK, "application/octet-stream", snap.Data)
}

// MouseEvent is one absolute pointer report. X/Y are 0..AbsMax.
type MouseEvent struct {
	X       int   `json:"x"`
	Y       int   `json:"y"`
	Buttons uint8 `json:"buttons"`
	DelayMs int   `json:"delayMs"`
}

type mouseRequest struct {
	Events []MouseEvent `json:"events"`
}

// handleMouse plays a list of absolute reports with real timing on the device,
// so a human-like path is not distorted by network jitter.
func (s *Server) handleMouse(c *gin.Context) {
	var req mouseRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, err)
		return
	}
	if err := checkCount(len(req.Events)); err != nil {
		badRequest(c, err)
		return
	}
	for i, e := range req.Events {
		if e.X < 0 || e.X > AbsMax || e.Y < 0 || e.Y > AbsMax {
			badRequest(c, fmt.Errorf("event %d: x/y must be within 0..%d", i, AbsMax))
			return
		}
		if err := checkDelay(e.DelayMs); err != nil {
			badRequest(c, fmt.Errorf("event %d: %w", i, err))
			return
		}
	}
	s.play(c, len(req.Events), func(i int) (int, error) {
		e := req.Events[i]
		return e.DelayMs, s.backend.AbsMouse(e.X, e.Y, e.Buttons&0x1f)
	}, nil)
}

// WheelEvent is one wheel report. Positive DY scrolls up, positive DX right.
type WheelEvent struct {
	DY      int `json:"dy"`
	DX      int `json:"dx"`
	DelayMs int `json:"delayMs"`
}

type wheelRequest struct {
	Events []WheelEvent `json:"events"`
}

func (s *Server) handleWheel(c *gin.Context) {
	var req wheelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, err)
		return
	}
	if err := checkCount(len(req.Events)); err != nil {
		badRequest(c, err)
		return
	}
	for i, e := range req.Events {
		if e.DY < -127 || e.DY > 127 || e.DX < -127 || e.DX > 127 {
			badRequest(c, fmt.Errorf("event %d: dy/dx must be within -127..127", i))
			return
		}
		if err := checkDelay(e.DelayMs); err != nil {
			badRequest(c, fmt.Errorf("event %d: %w", i, err))
			return
		}
	}
	s.play(c, len(req.Events), func(i int) (int, error) {
		e := req.Events[i]
		return e.DelayMs, s.backend.Wheel(int8(e.DY), int8(e.DX))
	}, nil)
}

// KeyEvent is one keyboard report: a modifier bitmask and up to 6 HID usage
// codes held down. An empty Keys with Modifier 0 releases everything.
type KeyEvent struct {
	Modifier byte  `json:"modifier"`
	Keys     []int `json:"keys"`
	DelayMs  int   `json:"delayMs"`
}

type keysRequest struct {
	Events []KeyEvent `json:"events"`
}

// handleKeys plays keyboard reports and always ends with all keys released, so
// a failed or cancelled request never leaves a key stuck down on the host.
func (s *Server) handleKeys(c *gin.Context) {
	var req keysRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequest(c, err)
		return
	}
	if err := checkCount(len(req.Events)); err != nil {
		badRequest(c, err)
		return
	}
	reports := make([][]byte, len(req.Events))
	for i, e := range req.Events {
		if len(e.Keys) > hidKeyBufSize {
			badRequest(c, fmt.Errorf("event %d: at most %d keys", i, hidKeyBufSize))
			return
		}
		if err := checkDelay(e.DelayMs); err != nil {
			badRequest(c, fmt.Errorf("event %d: %w", i, err))
			return
		}
		keys := make([]byte, hidKeyBufSize)
		for j, k := range e.Keys {
			if k < 0 || k > 255 {
				badRequest(c, fmt.Errorf("event %d: key codes must be 0..255", i))
				return
			}
			keys[j] = byte(k)
		}
		reports[i] = keys
	}
	release := func() { _ = s.backend.Keyboard(0, make([]byte, hidKeyBufSize)) }
	s.play(c, len(req.Events), func(i int) (int, error) {
		e := req.Events[i]
		return e.DelayMs, s.backend.Keyboard(e.Modifier, reports[i])
	}, release)
}

// play runs n steps under the input lock, honouring each step's delay and the
// request context (client disconnect cancels the rest).
func (s *Server) play(c *gin.Context, n int, step func(i int) (delayMs int, err error), finally func()) {
	s.inputMu.Lock()
	defer s.inputMu.Unlock()
	if finally != nil {
		defer finally()
	}
	ctx := c.Request.Context()
	for i := 0; i < n; i++ {
		delay, err := step(i)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error(), "played": i})
			return
		}
		if delay > 0 {
			if err := s.sleep(ctx, time.Duration(delay)*time.Millisecond); err != nil {
				c.JSON(499, gin.H{"error": "cancelled", "played": i + 1})
				return
			}
		}
	}
	c.JSON(http.StatusOK, gin.H{"played": n})
}

func checkCount(n int) error {
	if n == 0 {
		return fmt.Errorf("events must not be empty")
	}
	if n > maxEvents {
		return fmt.Errorf("at most %d events per request", maxEvents)
	}
	return nil
}

func checkDelay(ms int) error {
	if ms < 0 || ms > maxEventDelay {
		return fmt.Errorf("delayMs must be within 0..%d", maxEventDelay)
	}
	return nil
}

func badRequest(c *gin.Context, err error) {
	c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
}

func ctxSleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
