//go:build linux && arm

package kvm

import (
	"reflect"
	"strconv"
	"testing"

	"github.com/jetkvm/kvm/internal/native"
	"github.com/jetkvm/kvm/internal/usbgadget"
	"github.com/pion/webrtc/v4"
)

type cuaRecordingNative struct {
	native.EmptyNativeInterface
	calls []string
}

func (n *cuaRecordingNative) VideoSetCodecType(codecType int) error {
	n.calls = append(n.calls, "set-codec:"+strconv.Itoa(codecType))
	return nil
}
func (n *cuaRecordingNative) VideoStart() error { n.calls = append(n.calls, "start"); return nil }
func (n *cuaRecordingNative) VideoStop() error  { n.calls = append(n.calls, "stop"); return nil }

func withCuaTestState(t *testing.T) *cuaRecordingNative {
	t.Helper()
	rec := &cuaRecordingNative{}
	origNative, origConfig := nativeInstance, config
	nativeInstance = rec
	config = &Config{VideoSleepAfterSec: -1}
	hostDisplayAdvertiseLock.Lock()
	origAdvertised := hostDisplayAdvertised
	hostDisplayAdvertised = true // avoid EDID writes
	hostDisplayAdvertiseLock.Unlock()
	usbStateLock.Lock()
	origUSB := usbState
	usbState = usbgadget.USBStateNotAttached
	usbStateLock.Unlock()
	videoPauseMu.Lock()
	origCodec := nativeVideoCodec
	nativeVideoCodec = -1
	videoPauseMu.Unlock()
	t.Cleanup(func() {
		cuaVideoLease.Release()
		videoPauseMu.Lock()
		nativeVideoCodec = origCodec
		videoPauseMu.Unlock()
		usbStateLock.Lock()
		usbState = origUSB
		usbStateLock.Unlock()
		hostDisplayAdvertiseLock.Lock()
		hostDisplayAdvertised = origAdvertised
		hostDisplayAdvertiseLock.Unlock()
		nativeInstance, config = origNative, origConfig
	})
	return rec
}

func TestCuaLeaseStartsH264WithoutSession(t *testing.T) {
	rec := withCuaTestState(t)
	if err := (cuaBackend{}).EnsureVideo(); err != nil {
		t.Fatal(err)
	}
	if want := []string{"set-codec:0", "start"}; !reflect.DeepEqual(rec.calls, want) {
		t.Fatalf("calls %v, want %v", rec.calls, want)
	}
}

func TestSessionWithOtherCodecRestartsCuaPipeline(t *testing.T) {
	rec := withCuaTestState(t)
	_ = (cuaBackend{}).EnsureVideo() // CUA runs H.264
	rec.calls = nil
	startNativeVideoForSession(&Session{codecMimeType: webrtc.MimeTypeH265})
	if want := []string{"stop", "set-codec:1", "start"}; !reflect.DeepEqual(rec.calls, want) {
		t.Fatalf("calls %v, want %v", rec.calls, want)
	}
}

func TestLastSessionDisconnectKeepsVideoForCua(t *testing.T) {
	rec := withCuaTestState(t)
	_ = (cuaBackend{}).EnsureVideo()
	rec.calls = nil
	onLastSessionDisconnected()
	for _, c := range rec.calls {
		if c == "stop" {
			t.Fatalf("video stopped while CUA lease active: %v", rec.calls)
		}
	}
	cuaVideoLease.Release()
	if len(rec.calls) == 0 || rec.calls[len(rec.calls)-1] != "stop" {
		t.Fatalf("lease release must stop video, calls %v", rec.calls)
	}
}

func resetVideoPause(t *testing.T) {
	t.Cleanup(func() {
		videoPauseMu.Lock()
		videoPausedBy, videoPauseHidden = nil, false
		videoPauseMu.Unlock()
	})
}

func TestHiddenTabPauseDoesNotStopCua(t *testing.T) {
	rec := withCuaTestState(t)
	resetVideoPause(t)
	sess := &Session{}

	// A tab hidden before the CUA arrives: the CUA starts video anyway.
	_ = rpcSetVideoStreamPaused(sess, true, "hidden")
	rec.calls = nil
	if err := (cuaBackend{}).EnsureVideo(); err != nil {
		t.Fatal(err)
	}
	if want := []string{"set-codec:0", "start"}; !reflect.DeepEqual(rec.calls, want) {
		t.Fatalf("calls %v, want %v", rec.calls, want)
	}

	// A tab hidden while the CUA is active does not stop video.
	rec.calls = nil
	_ = rpcSetVideoStreamPaused(sess, true, "hidden")
	if len(rec.calls) != 0 {
		t.Fatalf("hidden pause touched the pipeline: %v", rec.calls)
	}

	// A virtual-media pause does, and the CUA reports it.
	_ = rpcSetVideoStreamPaused(sess, true, "")
	if len(rec.calls) == 0 || rec.calls[len(rec.calls)-1] != "stop" {
		t.Fatalf("media pause must stop video, calls %v", rec.calls)
	}
	if err := (cuaBackend{}).EnsureVideo(); err == nil {
		t.Fatal("EnsureVideo must fail while a media pause holds the pipeline")
	}
}
