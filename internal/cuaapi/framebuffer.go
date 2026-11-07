// Package cuaapi is the device side of the computer-using agent (CUA).
//
// The device stays "dumb eyes and hands": it hands out the most recent encoded
// video (as a decodable group of pictures) and forwards mouse, wheel and
// keyboard events to the USB HID gadget. Parsing the screen and deciding what to
// do happens off-device, in the cua/ Python service.
//
// This package has no device dependencies so it can be unit-tested on any host;
// the glue to the native video pipeline and the HID gadget lives in the root
// package (cua_api.go).
package cuaapi

import (
	"bytes"
	"sync"
	"time"
)

// Codec of the encoded stream, matching native.VideoSetCodecType.
type Codec int

const (
	CodecH264 Codec = 0
	CodecH265 Codec = 1
)

func (c Codec) String() string {
	if c == CodecH265 {
		return "h265"
	}
	return "h264"
}

// maxGOPBytes caps the buffer. A GOP is ~0.5 s of video, so this is only hit
// with a broken stream (no keyframes); the buffer is then dropped until the next
// keyframe instead of growing without bound.
const maxGOPBytes = 8 << 20

// FrameBuffer keeps every access unit since the last keyframe, so a client can
// decode the latest picture without a live WebRTC session.
type FrameBuffer struct {
	mu        sync.Mutex
	cond      *sync.Cond
	codec     Codec
	gop       [][]byte // gop[0] is a keyframe once haveKey is true
	gopBytes  int
	haveKey   bool
	params    map[int][]byte // cached VPS/SPS/PPS by NAL type
	seq       uint64         // increments on every pushed frame
	lastFrame time.Time
}

func NewFrameBuffer() *FrameBuffer {
	b := &FrameBuffer{params: map[int][]byte{}}
	b.cond = sync.NewCond(&b.mu)
	return b
}

// SetCodec switches the expected bitstream format and drops buffered data when
// it changes, since NAL parsing differs between H.264 and H.265.
func (b *FrameBuffer) SetCodec(c Codec) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.codec == c {
		return
	}
	b.codec = c
	b.resetLocked()
	b.params = map[int][]byte{}
}

func (b *FrameBuffer) Codec() Codec {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.codec
}

// Reset drops buffered frames, e.g. when the encoder stops.
func (b *FrameBuffer) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.resetLocked()
}

func (b *FrameBuffer) resetLocked() {
	b.gop = nil
	b.gopBytes = 0
	b.haveKey = false
}

// Push adds one encoded access unit (Annex-B). It is called from the video hot
// path, so it only scans NAL headers and copies the frame.
func (b *FrameBuffer) Push(frame []byte) {
	if len(frame) == 0 {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()

	types := nalTypes(frame, b.codec)
	key := false
	hasParams := false
	for _, n := range types {
		if isKeyframeNAL(n.typ, b.codec) {
			key = true
		}
		if isParamNAL(n.typ, b.codec) {
			hasParams = true
			b.params[n.typ] = append([]byte(nil), n.data...)
		}
	}

	if key {
		b.resetLocked()
		b.haveKey = true
		if !hasParams {
			// Some encoders send parameter sets only once; prepend the cached
			// ones so the GOP is decodable on its own.
			if ps := b.paramsLocked(); len(ps) > 0 {
				b.gop = append(b.gop, ps)
				b.gopBytes += len(ps)
			}
		}
	} else if !b.haveKey {
		b.seq++
		b.lastFrame = time.Now()
		b.cond.Broadcast()
		return // nothing decodable before the first keyframe
	}

	cp := append([]byte(nil), frame...)
	b.gop = append(b.gop, cp)
	b.gopBytes += len(cp)
	if b.gopBytes > maxGOPBytes {
		b.resetLocked()
	}
	b.seq++
	b.lastFrame = time.Now()
	b.cond.Broadcast()
}

func (b *FrameBuffer) paramsLocked() []byte {
	var order []int
	if b.codec == CodecH265 {
		order = []int{32, 33, 34}
	} else {
		order = []int{7, 8}
	}
	var out bytes.Buffer
	for _, t := range order {
		if p, ok := b.params[t]; ok {
			out.Write(startCode)
			out.Write(p)
		}
	}
	return out.Bytes()
}

// Snapshot is a self-contained, decodable chunk of the stream; decoding it and
// keeping the last picture yields the most recent screen.
type Snapshot struct {
	Data      []byte
	Codec     Codec
	Seq       uint64
	Frames    int
	LastFrame time.Time
}

// Snapshot returns the current GOP, or ok=false when no keyframe has arrived yet.
func (b *FrameBuffer) Snapshot() (Snapshot, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.snapshotLocked()
}

func (b *FrameBuffer) snapshotLocked() (Snapshot, bool) {
	if !b.haveKey || len(b.gop) == 0 {
		return Snapshot{}, false
	}
	data := make([]byte, 0, b.gopBytes)
	for _, f := range b.gop {
		data = append(data, f...)
	}
	return Snapshot{Data: data, Codec: b.codec, Seq: b.seq, Frames: len(b.gop), LastFrame: b.lastFrame}, true
}

// WaitSnapshot blocks until a decodable snapshot newer than afterSeq exists or
// the timeout passes. afterSeq=0 accepts any snapshot.
func (b *FrameBuffer) WaitSnapshot(afterSeq uint64, timeout time.Duration) (Snapshot, bool) {
	deadline := time.Now().Add(timeout)
	timer := time.AfterFunc(timeout, func() {
		b.mu.Lock()
		b.cond.Broadcast()
		b.mu.Unlock()
	})
	defer timer.Stop()

	b.mu.Lock()
	defer b.mu.Unlock()
	for {
		if s, ok := b.snapshotLocked(); ok && s.Seq > afterSeq {
			return s, true
		}
		if !time.Now().Before(deadline) {
			return Snapshot{}, false
		}
		b.cond.Wait()
	}
}

var startCode = []byte{0, 0, 0, 1}

type nal struct {
	typ  int
	data []byte // NAL unit without start code
}

// nalTypes splits an Annex-B buffer into NAL units.
func nalTypes(buf []byte, codec Codec) []nal {
	var out []nal
	starts := findStartCodes(buf)
	for i, s := range starts {
		end := len(buf)
		if i+1 < len(starts) {
			end = starts[i+1].pos
		}
		body := buf[s.pos+s.len : end]
		// trailing zero bytes belong to the next start code
		for len(body) > 0 && body[len(body)-1] == 0 {
			body = body[:len(body)-1]
		}
		if len(body) == 0 {
			continue
		}
		var t int
		if codec == CodecH265 {
			t = int(body[0]>>1) & 0x3f
		} else {
			t = int(body[0]) & 0x1f
		}
		out = append(out, nal{typ: t, data: body})
	}
	return out
}

type startPos struct{ pos, len int }

func findStartCodes(buf []byte) []startPos {
	var out []startPos
	for i := 0; i+2 < len(buf); i++ {
		if buf[i] == 0 && buf[i+1] == 0 {
			if buf[i+2] == 1 {
				out = append(out, startPos{i, 3})
				i += 2
			} else if i+3 < len(buf) && buf[i+2] == 0 && buf[i+3] == 1 {
				out = append(out, startPos{i, 4})
				i += 3
			}
		}
	}
	return out
}

func isKeyframeNAL(t int, c Codec) bool {
	if c == CodecH265 {
		return t >= 16 && t <= 21 // IRAP pictures (BLA/IDR/CRA)
	}
	return t == 5
}

func isParamNAL(t int, c Codec) bool {
	if c == CodecH265 {
		return t == 32 || t == 33 || t == 34
	}
	return t == 7 || t == 8
}
