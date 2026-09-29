package vpnroute

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"sync"
	"syscall"
	"time"

	"github.com/metacubex/mihomo/log"
)

// Only connection establishment is retried. No application bytes are replayed.
const fallbackCapacity = 1024
const fallbackCooldown = 60 * time.Second

type fallbackEntry struct {
	until, touched time.Time
	probing        bool
	generation     uint64
}
type fallbackCache struct {
	mu      sync.Mutex
	entries map[string]*fallbackEntry
}

var fallbacks = fallbackCache{entries: make(map[string]*fallbackEntry)}

func (c *fallbackCache) begin(key string, now time.Time) (bool, *fallbackEntry, uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.entries[key]
	if e == nil {
		if len(c.entries) >= fallbackCapacity {
			var oldest string
			var stamp time.Time
			for k, v := range c.entries {
				if !v.probing && (oldest == "" || v.touched.Before(stamp)) {
					oldest, stamp = k, v.touched
				}
			}
			if oldest == "" {
				return false, nil, 0
			}
			delete(c.entries, oldest)
		}
		e = &fallbackEntry{}
		c.entries[key] = e
	}
	e.touched = now
	if !e.until.IsZero() && (now.Before(e.until) || e.probing) {
		return true, e, e.generation
	}
	e.probing = !e.until.IsZero()
	e.generation++
	return false, e, e.generation
}
func (c *fallbackCache) finish(key string, entry *fallbackEntry, generation uint64, physical bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if entry == nil || c.entries[key] != entry || entry.generation != generation {
		return
	}
	entry.probing = false
	entry.touched = time.Now()
	if physical {
		entry.until = entry.touched.Add(fallbackCooldown)
	} else {
		entry.until = time.Time{}
	}
}

var privateTargets = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"), netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"), netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"), netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.0.0.0/24"), netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("192.88.99.0/24"), netip.MustParsePrefix("192.168.0.0/16"),
	netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("224.0.0.0/3"),
	netip.MustParsePrefix("2001::/23"), netip.MustParsePrefix("2001:db8::/32"),
	netip.MustParsePrefix("2002::/16"), netip.MustParsePrefix("3fff::/20"),
}

func PublicTarget(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() || ip.Zone() != "" {
		return false
	}
	if ip.Is6() && !netip.MustParsePrefix("2000::/3").Contains(ip) {
		return false
	}
	for _, prefix := range privateTargets {
		if prefix.Contains(ip) {
			return false
		}
	}
	return true
}

func fallbackError(err error) bool {
	// Route/IPC/policy failures are never treated as a network outage.
	var op *net.OpError
	if !errors.As(err, &op) || op.Op != "dial" {
		return false
	}
	if op.Timeout() {
		return true
	}
	for _, code := range []syscall.Errno{syscall.ENETUNREACH, syscall.EHOSTUNREACH, 10051, 10065, 10060} {
		if errors.Is(op.Err, code) {
			return true
		}
	}
	return false
}

func DialWithFallback(ctx context.Context, key string, primary, physical func(context.Context) (net.Conn, error)) (net.Conn, error) {
	if _, ok := ctx.Deadline(); !ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	skip, entry, generation := fallbacks.begin(key, time.Now())
	if skip {
		return physical(ctx)
	}
	successfulPhysical := false
	defer func() { fallbacks.finish(key, entry, generation, successfulPhysical) }()
	// Reserve part of the caller's existing budget for one physical attempt.
	budget := 3 * time.Second
	if deadline, ok := ctx.Deadline(); ok {
		if remaining := time.Until(deadline) / 2; remaining < budget {
			budget = remaining
		}
	}
	primaryCtx, cancel := context.WithTimeout(ctx, budget)
	c, err := primary(primaryCtx)
	cancel()
	if err == nil {
		return c, nil
	}
	if c != nil {
		_ = c.Close()
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if !fallbackError(err) {
		return nil, err
	}
	log.Infoln("[vpn-fallback] TCP connect failed target=%s; physical attempt submitted", key)
	result, physicalErr := physical(ctx)
	if physicalErr != nil {
		if result != nil {
			_ = result.Close()
		}
		log.Warnln("[vpn-fallback] physical attempt failed target=%s: %v", key, physicalErr)
		return nil, errors.Join(err, physicalErr)
	}
	if ctx.Err() != nil {
		_ = result.Close()
		return nil, ctx.Err()
	}
	successfulPhysical = true
	log.Infoln("[vpn-fallback] physical connect succeeded target=%s cooldown=60s", key)
	return result, nil
}
