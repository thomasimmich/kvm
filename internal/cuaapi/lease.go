package cuaapi

import (
	"sync"
	"sync/atomic"
	"time"
)

// Lease keeps the video pipeline running while a CUA client is polling, even
// with no browser connected. Every Touch extends it; after idle time without a
// Touch it is released.
type Lease struct {
	idle      time.Duration
	onAcquire func() error
	onRelease func()

	mu     sync.Mutex
	timer  *time.Timer
	active atomic.Bool
}

func NewLease(idle time.Duration, onAcquire func() error, onRelease func()) *Lease {
	return &Lease{idle: idle, onAcquire: onAcquire, onRelease: onRelease}
}

// Active is lock-free so it can be read from code that holds other locks
// (e.g. the host display advertisement lock).
func (l *Lease) Active() bool { return l.active.Load() }

// Touch acquires the lease if needed and pushes the idle deadline out.
func (l *Lease) Touch() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.active.Load() {
		// Mark active before acquiring so the acquire callback sees it (it may
		// re-evaluate display advertisement, which reads Active()).
		l.active.Store(true)
		if l.onAcquire != nil {
			if err := l.onAcquire(); err != nil {
				l.active.Store(false)
				return err
			}
		}
	}
	if l.timer != nil {
		l.timer.Stop()
	}
	l.timer = time.AfterFunc(l.idle, l.expire)
	return nil
}

func (l *Lease) expire() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.active.Load() {
		return
	}
	l.active.Store(false)
	l.timer = nil
	if l.onRelease != nil {
		l.onRelease()
	}
}

// Release ends the lease immediately.
func (l *Lease) Release() {
	l.mu.Lock()
	if l.timer != nil {
		l.timer.Stop()
		l.timer = nil
	}
	l.mu.Unlock()
	l.expire()
}
