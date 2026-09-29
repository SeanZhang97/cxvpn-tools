package vpnroute

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"sync"
	"syscall"
	"testing"
	"time"
)

type fallbackTestConn struct{ closed, writes int }

func (*fallbackTestConn) Read([]byte) (int, error)         { return 0, io.EOF }
func (c *fallbackTestConn) Write(b []byte) (int, error)    { c.writes++; return len(b), nil }
func (c *fallbackTestConn) Close() error                   { c.closed++; return nil }
func (*fallbackTestConn) LocalAddr() net.Addr              { return &net.TCPAddr{} }
func (*fallbackTestConn) RemoteAddr() net.Addr             { return &net.TCPAddr{} }
func (*fallbackTestConn) SetDeadline(time.Time) error      { return nil }
func (*fallbackTestConn) SetReadDeadline(time.Time) error  { return nil }
func (*fallbackTestConn) SetWriteDeadline(time.Time) error { return nil }

func fallbackDialError(err error) error { return &net.OpError{Op: "dial", Net: "tcp", Err: err} }
func fallbackTestKey(t *testing.T) string {
	t.Helper()
	key := t.Name()
	t.Cleanup(func() { fallbacks.mu.Lock(); delete(fallbacks.entries, key); fallbacks.mu.Unlock() })
	return key
}

func TestFallbackErrorClassification(t *testing.T) {
	for _, tc := range []struct {
		name    string
		err     error
		allowed bool
	}{
		{"dial timeout", fallbackDialError(os.ErrDeadlineExceeded), true},
		{"wrapped timeout", fmt.Errorf("connect: %w", fallbackDialError(context.DeadlineExceeded)), true},
		{"network unreachable", fallbackDialError(syscall.ENETUNREACH), true},
		{"host unreachable", fallbackDialError(syscall.EHOSTUNREACH), true},
		{"Windows network unreachable", fallbackDialError(syscall.Errno(10051)), true},
		{"Windows host unreachable", fallbackDialError(syscall.Errno(10065)), true},
		{"Windows timeout", fallbackDialError(syscall.Errno(10060)), true},
		{"nil", nil, false},
		{"cancelled", fallbackDialError(context.Canceled), false},
		{"refused", fallbackDialError(syscall.ECONNREFUSED), false},
		{"connection reset", fallbackDialError(syscall.ECONNRESET), false},
		{"permission", fallbackDialError(syscall.EACCES), false},
		{"read timeout", &net.OpError{Op: "read", Net: "tcp", Err: os.ErrDeadlineExceeded}, false},
		{"write timeout", &net.OpError{Op: "write", Net: "tcp", Err: os.ErrDeadlineExceeded}, false},
		{"route IPC deadline", fmt.Errorf("VPN route prepare: %w", context.DeadlineExceeded), false},
		{"policy rejected", errors.New("VPN route prepare: invalid VPN policy"), false},
		{"HTTP failure", errors.New("HTTP 403 Forbidden"), false},
		{"TLS failure", errors.New("x509: certificate signed by unknown authority"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := fallbackError(tc.err); got != tc.allowed {
				t.Fatalf("allowed=%v, want %v for %v", got, tc.allowed, tc.err)
			}
		})
	}
}

func TestFallbackPublicTargetBoundaries(t *testing.T) {
	for _, value := range []string{"1.1.1.1", "45.113.20.1", "100.63.255.255", "100.128.0.0", "172.15.255.255", "172.32.0.0", "223.255.255.255", "2606:4700::1111", "2001:4860:4860::8888", "::ffff:1.1.1.1"} {
		if !PublicTarget(netip.MustParseAddr(value)) {
			t.Errorf("public target rejected: %s", value)
		}
	}
	for _, value := range []string{"0.0.0.0", "0.1.2.3", "10.1.2.3", "100.64.0.0", "100.127.255.255", "127.0.0.1", "169.254.1.1", "172.16.0.0", "172.31.255.255", "192.0.0.1", "192.0.2.1", "192.88.99.1", "192.168.1.1", "198.18.0.1", "198.19.255.255", "198.51.100.1", "203.0.113.1", "224.0.0.1", "255.255.255.255", "::", "::1", "::ffff:192.168.1.1", "64:ff9b::a00:1", "fd00::1", "fe80::1", "ff02::1", "2001::1", "2001:1ff::1", "2001:db8::1", "2002::1", "3fff::1", "3fff:fff::1", "2606:4700::1111%eth0"} {
		if PublicTarget(netip.MustParseAddr(value)) {
			t.Errorf("private/special target allowed: %s", value)
		}
	}
	if PublicTarget(netip.Addr{}) {
		t.Error("invalid target allowed")
	}
}

func TestFallbackRetriesOnlyEstablishmentAndReservesDeadline(t *testing.T) {
	key := fallbackTestKey(t)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	parentDeadline, _ := ctx.Deadline()
	failed, physical := &fallbackTestConn{}, &fallbackTestConn{}
	primaryCalls, physicalCalls := 0, 0
	c, err := DialWithFallback(ctx, key, func(attempt context.Context) (net.Conn, error) {
		primaryCalls++
		deadline, ok := attempt.Deadline()
		if !ok || !deadline.Before(parentDeadline) {
			t.Error("no time reserved for physical attempt")
		}
		return failed, fallbackDialError(syscall.ENETUNREACH)
	}, func(attempt context.Context) (net.Conn, error) {
		physicalCalls++
		deadline, ok := attempt.Deadline()
		if !ok || !deadline.Equal(parentDeadline) {
			t.Error("physical attempt changed caller deadline")
		}
		return physical, nil
	})
	if err != nil || c != physical || primaryCalls != 1 || physicalCalls != 1 {
		t.Fatalf("unexpected result %v, calls=%d/%d", err, primaryCalls, physicalCalls)
	}
	defer c.Close()
	if failed.closed != 1 || failed.writes != 0 || physical.writes != 0 {
		t.Fatal("failed socket leaked or application bytes replayed")
	}
}

func TestFallbackSuppliesDeadlineWhenCallerHasNone(t *testing.T) {
	key := fallbackTestKey(t)
	c, err := DialWithFallback(context.Background(), key,
		func(context.Context) (net.Conn, error) { return nil, fallbackDialError(syscall.ENETUNREACH) },
		func(ctx context.Context) (net.Conn, error) {
			deadline, ok := ctx.Deadline()
			if !ok || time.Until(deadline) > 10*time.Second {
				t.Fatal("unbounded physical attempt")
			}
			return &fallbackTestConn{}, nil
		})
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
}

func TestFallbackDoesNotRetryPolicyProtocolOrCancellationErrors(t *testing.T) {
	for _, failure := range []error{errors.New("route service unavailable"), context.DeadlineExceeded, fallbackDialError(context.Canceled), fallbackDialError(syscall.ECONNREFUSED), &net.OpError{Op: "read", Err: os.ErrDeadlineExceeded}} {
		t.Run(failure.Error(), func(t *testing.T) {
			key := fallbackTestKey(t)
			c, err := DialWithFallback(context.Background(), key, func(context.Context) (net.Conn, error) { return nil, failure }, func(context.Context) (net.Conn, error) {
				t.Error("unexpected physical retry")
				return nil, errors.New("unexpected")
			})
			if c != nil || !errors.Is(err, failure) {
				t.Fatalf("original failure lost: %v", err)
			}
		})
	}
}

func TestFallbackParentCancellationStopsAttemptsAndClosesLatePhysical(t *testing.T) {
	t.Run("before primary", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		unexpected := func(context.Context) (net.Conn, error) {
			t.Error("dial after parent cancellation")
			return nil, errors.New("unexpected")
		}
		c, err := DialWithFallback(ctx, fallbackTestKey(t), unexpected, unexpected)
		if c != nil || !errors.Is(err, context.Canceled) {
			t.Fatalf("unexpected cancellation result: %v", err)
		}
	})
	t.Run("during primary", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		failed := &fallbackTestConn{}
		c, err := DialWithFallback(ctx, fallbackTestKey(t), func(context.Context) (net.Conn, error) {
			cancel()
			return failed, fallbackDialError(os.ErrDeadlineExceeded)
		}, func(context.Context) (net.Conn, error) {
			t.Error("physical after parent cancellation")
			return nil, nil
		})
		if c != nil || !errors.Is(err, context.Canceled) || failed.closed != 1 {
			t.Fatalf("cancelled primary leaked: %v closed=%d", err, failed.closed)
		}
	})
	t.Run("during physical", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		late := &fallbackTestConn{}
		c, err := DialWithFallback(ctx, fallbackTestKey(t), func(context.Context) (net.Conn, error) { return nil, fallbackDialError(syscall.EHOSTUNREACH) }, func(context.Context) (net.Conn, error) { cancel(); return late, nil })
		if c != nil || !errors.Is(err, context.Canceled) || late.closed != 1 {
			t.Fatalf("late physical leaked: %v closed=%d", err, late.closed)
		}
	})
}

func TestFallbackBothFailuresPreserveErrorsAndDoNotStartCooldown(t *testing.T) {
	key := fallbackTestKey(t)
	primaryErr, physicalErr := fallbackDialError(syscall.ENETUNREACH), errors.New("physical unavailable")
	failed := &fallbackTestConn{}
	c, err := DialWithFallback(context.Background(), key, func(context.Context) (net.Conn, error) { return nil, primaryErr }, func(context.Context) (net.Conn, error) { return failed, physicalErr })
	if c != nil || !errors.Is(err, primaryErr) || !errors.Is(err, physicalErr) || failed.closed != 1 {
		t.Fatalf("failure cleanup lost: %v closed=%d", err, failed.closed)
	}
	skip, entry, generation := fallbacks.begin(key, time.Now())
	defer fallbacks.finish(key, entry, generation, false)
	if skip {
		t.Fatal("failed physical connection started cooldown")
	}
}

func TestFallbackCooldownIsolatesTargetsAndRecoversVPN(t *testing.T) {
	key := fallbackTestKey(t)
	otherKey := key + "-different-target"
	t.Cleanup(func() { fallbacks.mu.Lock(); delete(fallbacks.entries, otherKey); fallbacks.mu.Unlock() })
	primaryCalls, physicalCalls := 0, 0
	primary := func(context.Context) (net.Conn, error) {
		primaryCalls++
		return nil, fallbackDialError(syscall.ENETUNREACH)
	}
	physical := func(context.Context) (net.Conn, error) { physicalCalls++; return &fallbackTestConn{}, nil }
	for i := 0; i < 2; i++ {
		c, err := DialWithFallback(context.Background(), key, primary, physical)
		if err != nil {
			t.Fatal(err)
		}
		c.Close()
	}
	if primaryCalls != 1 || physicalCalls != 2 {
		t.Fatalf("cooldown did not bypass unhealthy VPN: %d/%d", primaryCalls, physicalCalls)
	}
	good := func(context.Context) (net.Conn, error) { primaryCalls++; return &fallbackTestConn{}, nil }
	c, err := DialWithFallback(context.Background(), otherKey, good, physical)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	if primaryCalls != 2 || physicalCalls != 2 {
		t.Fatal("cooldown affected a different target")
	}
	fallbacks.mu.Lock()
	fallbacks.entries[key].until = time.Now().Add(-time.Second)
	fallbacks.mu.Unlock()
	for i := 0; i < 2; i++ {
		c, err = DialWithFallback(context.Background(), key, good, physical)
		if err != nil {
			t.Fatal(err)
		}
		c.Close()
	}
	if primaryCalls != 4 || physicalCalls != 2 {
		t.Fatal("recovered VPN did not remain primary")
	}
}

func TestFallbackOnlyOneExpiredTargetProbeRuns(t *testing.T) {
	cache := fallbackCache{entries: make(map[string]*fallbackEntry)}
	now := time.Now()
	_, entry, generation := cache.begin("target", now)
	cache.finish("target", entry, generation, true)
	entry.until = now.Add(-time.Second)
	skip, probe, probeGeneration := cache.begin("target", now)
	if skip || !probe.probing {
		t.Fatal("expired target did not enter probe state")
	}
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			skip, _, generation := cache.begin("target", now)
			if !skip || generation != probeGeneration {
				t.Error("parallel VPN probe admitted")
			}
		}()
	}
	wg.Wait()
	cache.finish("target", probe, probeGeneration, false)
	skip, _, _ = cache.begin("target", now)
	if skip {
		t.Fatal("successful probe did not restore VPN")
	}
}

func TestFallbackLateResultCannotOverrideNewerOrEvictedDecision(t *testing.T) {
	cache := fallbackCache{entries: make(map[string]*fallbackEntry)}
	now := time.Now()
	_, old, oldGeneration := cache.begin("target", now)
	_, current, currentGeneration := cache.begin("target", now)
	cache.finish("target", current, currentGeneration, true)
	cache.finish("target", old, oldGeneration, false)
	if skip, _, _ := cache.begin("target", now); !skip {
		t.Fatal("late success cleared newer physical cooldown")
	}
	delete(cache.entries, "target")
	_, replacement, replacementGeneration := cache.begin("target", now)
	cache.finish("target", old, currentGeneration, true)
	if !replacement.until.IsZero() {
		t.Fatal("evicted result changed new target state")
	}
	cache.finish("target", replacement, replacementGeneration, false)
}

func TestFallbackCapacityEvictsIdleEntriesButNotActiveProbe(t *testing.T) {
	cache := fallbackCache{entries: make(map[string]*fallbackEntry)}
	now := time.Now()
	for i := 0; i < fallbackCapacity; i++ {
		cache.begin(fmt.Sprint(i), now.Add(time.Duration(i)*time.Second))
	}
	cache.entries["0"].probing = true
	_, evicted := cache.entries["1"]
	if !evicted {
		t.Fatal("fixture missing")
	}
	cache.begin("new", now.Add(time.Hour))
	if len(cache.entries) != fallbackCapacity || cache.entries["0"] == nil || cache.entries["1"] != nil {
		t.Fatal("capacity eviction removed active probe or exceeded bound")
	}
	for _, entry := range cache.entries {
		entry.probing = true
	}
	skip, entry, generation := cache.begin("capacity-full", now)
	if skip || entry != nil || generation != 0 || len(cache.entries) != fallbackCapacity {
		t.Fatal("full cache admitted unbounded state or forced physical fallback")
	}
	cache.finish("capacity-full", entry, generation, true)
}
