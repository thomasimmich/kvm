package cuaapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

// --- frame buffer ----------------------------------------------------------

func h264(nals ...byte) []byte {
	var b []byte
	for _, n := range nals {
		b = append(b, 0, 0, 0, 1, n, 0xAA, 0xBB)
	}
	return b
}

const (
	sps = 0x67 // nal type 7
	pps = 0x68 // nal type 8
	idr = 0x65 // nal type 5
	pf  = 0x41 // nal type 1 (P slice)
)

func TestFrameBufferDropsFramesBeforeFirstKeyframe(t *testing.T) {
	b := NewFrameBuffer()
	b.Push(h264(pf))
	if _, ok := b.Snapshot(); ok {
		t.Fatal("snapshot without keyframe must not be decodable")
	}
	b.Push(h264(sps, pps, idr))
	b.Push(h264(pf))
	s, ok := b.Snapshot()
	if !ok || s.Frames != 2 {
		t.Fatalf("got ok=%v frames=%d, want 2 frames", ok, s.Frames)
	}
	if !bytes.Equal(s.Data, append(h264(sps, pps, idr), h264(pf)...)) {
		t.Fatalf("unexpected data %x", s.Data)
	}
}

func TestFrameBufferKeyframeResetsGOP(t *testing.T) {
	b := NewFrameBuffer()
	b.Push(h264(sps, pps, idr))
	b.Push(h264(pf))
	b.Push(h264(pf))
	b.Push(h264(sps, pps, idr))
	s, _ := b.Snapshot()
	if s.Frames != 1 {
		t.Fatalf("frames=%d, want 1 after new keyframe", s.Frames)
	}
}

func TestFrameBufferPrependsCachedParamsToBareKeyframe(t *testing.T) {
	b := NewFrameBuffer()
	b.Push(h264(sps, pps, idr))
	b.Push(h264(idr)) // keyframe without SPS/PPS
	s, _ := b.Snapshot()
	nals := nalTypes(s.Data, CodecH264)
	var got []int
	for _, n := range nals {
		got = append(got, n.typ)
	}
	if fmt.Sprint(got) != "[7 8 5]" {
		t.Fatalf("nal types = %v, want [7 8 5]", got)
	}
}

func TestFrameBufferH265Keyframe(t *testing.T) {
	b := NewFrameBuffer()
	b.SetCodec(CodecH265)
	// VPS(32)=0x40, SPS(33)=0x42, PPS(34)=0x44, IDR_W_RADL(19)=0x26, TRAIL_R(1)=0x02
	frame := func(hdr ...byte) []byte {
		var out []byte
		for _, h := range hdr {
			out = append(out, 0, 0, 1, h, 0x01, 0xAA)
		}
		return out
	}
	b.Push(frame(0x02))
	if _, ok := b.Snapshot(); ok {
		t.Fatal("no keyframe yet")
	}
	b.Push(frame(0x40, 0x42, 0x44, 0x26))
	b.Push(frame(0x02))
	if s, ok := b.Snapshot(); !ok || s.Frames != 2 || s.Codec != CodecH265 {
		t.Fatalf("got %+v ok=%v", s, ok)
	}
}

func TestFrameBufferCodecSwitchResets(t *testing.T) {
	b := NewFrameBuffer()
	b.Push(h264(sps, pps, idr))
	b.SetCodec(CodecH265)
	if _, ok := b.Snapshot(); ok {
		t.Fatal("codec switch must drop buffered frames")
	}
}

func TestWaitSnapshotAfterSeq(t *testing.T) {
	b := NewFrameBuffer()
	b.Push(h264(sps, pps, idr))
	s1, _ := b.Snapshot()
	go func() {
		time.Sleep(20 * time.Millisecond)
		b.Push(h264(pf))
	}()
	s2, ok := b.WaitSnapshot(s1.Seq, time.Second)
	if !ok || s2.Seq <= s1.Seq {
		t.Fatalf("expected newer snapshot, got ok=%v seq=%d (was %d)", ok, s2.Seq, s1.Seq)
	}
	if _, ok := b.WaitSnapshot(s2.Seq, 30*time.Millisecond); ok {
		t.Fatal("expected timeout without new frames")
	}
}

// --- lease -----------------------------------------------------------------

func TestLeaseAcquireExtendRelease(t *testing.T) {
	var acquired, released atomic.Int32
	l := NewLease(40*time.Millisecond, func() error { acquired.Add(1); return nil }, func() { released.Add(1) })
	for i := 0; i < 3; i++ {
		if err := l.Touch(); err != nil {
			t.Fatal(err)
		}
		time.Sleep(15 * time.Millisecond)
	}
	if acquired.Load() != 1 || !l.Active() {
		t.Fatalf("acquired=%d active=%v", acquired.Load(), l.Active())
	}
	time.Sleep(80 * time.Millisecond)
	if released.Load() != 1 || l.Active() {
		t.Fatalf("released=%d active=%v", released.Load(), l.Active())
	}
}

func TestLeaseAcquireError(t *testing.T) {
	l := NewLease(time.Second, func() error { return errors.New("boom") }, nil)
	if err := l.Touch(); err == nil || l.Active() {
		t.Fatal("failed acquire must leave lease inactive")
	}
}

// --- handlers --------------------------------------------------------------

type fakeBackend struct {
	mu     sync.Mutex
	calls  []string
	failAt int // fail the n-th HID call (1-based); 0 = never
	n      int
}

func (f *fakeBackend) record(s string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.n++
	f.calls = append(f.calls, s)
	if f.failAt > 0 && f.n == f.failAt {
		return errors.New("hid gone")
	}
	return nil
}
func (f *fakeBackend) VideoInfo() VideoInfo {
	return VideoInfo{Ready: true, Streaming: true, Width: 1920, Height: 1080, FPS: 60}
}
func (f *fakeBackend) EnsureVideo() error { return nil }
func (f *fakeBackend) AbsMouse(x, y int, b uint8) error {
	return f.record(fmt.Sprintf("mouse %d,%d,%d", x, y, b))
}
func (f *fakeBackend) Wheel(dy, dx int8) error { return f.record(fmt.Sprintf("wheel %d,%d", dy, dx)) }
func (f *fakeBackend) Keyboard(m byte, k []byte) error {
	return f.record(fmt.Sprintf("key %d %v", m, k))
}
func (f *fakeBackend) ActiveSessions() int { return 0 }

func newTestServer(t *testing.T, enabled bool) (*gin.Engine, *fakeBackend, *FrameBuffer) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	be := &fakeBackend{}
	fb := NewFrameBuffer()
	s := NewServer(be, fb, func() (bool, string) { return enabled, "secret" })
	s.sleep = func(ctx context.Context, d time.Duration) error { return ctx.Err() }
	r := gin.New()
	s.Register(r.Group("/api/cua"))
	return r, be, fb
}

func do(r *gin.Engine, method, path, token, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestAuth(t *testing.T) {
	r, _, _ := newTestServer(t, true)
	if w := do(r, "GET", "/api/cua/state", "", ""); w.Code != http.StatusUnauthorized {
		t.Fatalf("no token: %d", w.Code)
	}
	if w := do(r, "GET", "/api/cua/state", "wrong", ""); w.Code != http.StatusUnauthorized {
		t.Fatalf("wrong token: %d", w.Code)
	}
	if w := do(r, "GET", "/api/cua/state", "secret", ""); w.Code != http.StatusOK {
		t.Fatalf("good token: %d %s", w.Code, w.Body)
	}
	r2, _, _ := newTestServer(t, false)
	if w := do(r2, "GET", "/api/cua/state", "secret", ""); w.Code != http.StatusNotFound {
		t.Fatalf("disabled: %d", w.Code)
	}
}

func TestFrameEndpoint(t *testing.T) {
	r, _, fb := newTestServer(t, true)
	fb.Push(h264(sps, pps, idr))
	w := do(r, "GET", "/api/cua/frame", "secret", "")
	if w.Code != http.StatusOK {
		t.Fatalf("code %d %s", w.Code, w.Body)
	}
	if w.Header().Get("X-Codec") != "h264" || w.Header().Get("X-Width") != "1920" || w.Header().Get("X-Frame-Seq") != "1" {
		t.Fatalf("headers %v", w.Header())
	}
	if !bytes.Equal(w.Body.Bytes(), h264(sps, pps, idr)) {
		t.Fatalf("body %x", w.Body.Bytes())
	}
	if w := do(r, "GET", "/api/cua/frame?after=x", "secret", ""); w.Code != http.StatusBadRequest {
		t.Fatalf("bad after: %d", w.Code)
	}
}

func TestMouseValidationAndPlayback(t *testing.T) {
	r, be, _ := newTestServer(t, true)
	if w := do(r, "POST", "/api/cua/mouse", "secret", `{"events":[{"x":40000,"y":1}]}`); w.Code != http.StatusBadRequest {
		t.Fatalf("out of range: %d", w.Code)
	}
	if w := do(r, "POST", "/api/cua/mouse", "secret", `{"events":[]}`); w.Code != http.StatusBadRequest {
		t.Fatalf("empty: %d", w.Code)
	}
	w := do(r, "POST", "/api/cua/mouse", "secret", `{"events":[{"x":1,"y":2},{"x":3,"y":4,"buttons":1},{"x":3,"y":4,"buttons":0}]}`)
	if w.Code != http.StatusOK {
		t.Fatalf("code %d %s", w.Code, w.Body)
	}
	want := "[mouse 1,2,0 mouse 3,4,1 mouse 3,4,0]"
	if fmt.Sprint(be.calls) != want {
		t.Fatalf("calls %v, want %s", be.calls, want)
	}
}

func TestWheel(t *testing.T) {
	r, be, _ := newTestServer(t, true)
	if w := do(r, "POST", "/api/cua/wheel", "secret", `{"events":[{"dy":200}]}`); w.Code != http.StatusBadRequest {
		t.Fatalf("range: %d", w.Code)
	}
	w := do(r, "POST", "/api/cua/wheel", "secret", `{"events":[{"dy":-1},{"dx":2}]}`)
	if w.Code != http.StatusOK || fmt.Sprint(be.calls) != "[wheel -1,0 wheel 0,2]" {
		t.Fatalf("code %d calls %v", w.Code, be.calls)
	}
}

func TestKeysAlwaysReleaseEvenOnError(t *testing.T) {
	r, be, _ := newTestServer(t, true)
	be.failAt = 2
	w := do(r, "POST", "/api/cua/keys", "secret", `{"events":[{"modifier":2,"keys":[4]},{"keys":[5]},{"keys":[6]}]}`)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("code %d", w.Code)
	}
	var body map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["played"].(float64) != 1 {
		t.Fatalf("played %v", body["played"])
	}
	last := be.calls[len(be.calls)-1]
	if last != "key 0 [0 0 0 0 0 0]" {
		t.Fatalf("last call %q, want release", last)
	}
	if w := do(r, "POST", "/api/cua/keys", "secret", `{"events":[{"keys":[1,2,3,4,5,6,7]}]}`); w.Code != http.StatusBadRequest {
		t.Fatalf("too many keys: %d", w.Code)
	}
}
