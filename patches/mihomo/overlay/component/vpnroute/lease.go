// Package vpnroute prepares a Windows VPN route before a socket may send.
package vpnroute

import (
 "context"
 "errors"
 "fmt"
 "net"
 "net/netip"
 "os"
 "strconv"
 "sync"
 "sync/atomic"
 "time"

 "github.com/metacubex/mihomo/log"
)

type request struct { Op string `json:"op"`; Epoch string `json:"epoch"`; ID string `json:"id"`; VPN string `json:"vpn,omitempty"`; IP string `json:"ip,omitempty"` }
type Lease struct { id string; once sync.Once; Interface string }
var sequence atomic.Uint64
var slots = make(chan struct{}, 32)
var exchange = exchangeNative
var pendingMu sync.Mutex
var pending = map[string]int{}
var reaper sync.Once
var wake = make(chan struct{},1)

func call(ctx context.Context, req request) (string,error) {
 ctx, cancel := context.WithTimeout(ctx, 2*time.Second); defer cancel()
 select { case slots <- struct{}{}: defer func(){<-slots}(); case <-ctx.Done(): return "",ctx.Err() }
 req.Epoch = os.Getenv("CXVPN_ROUTE_EPOCH")
 if req.Epoch == "" { return "",errors.New("managed VPN route service generation unavailable") }
 return exchange(ctx, req)
}

func Acquire(ctx context.Context, vpn string, ip netip.Addr) (*Lease, error) {
 if vpn == "" || !ip.IsValid() { return nil, errors.New("invalid managed VPN target") }
 id := strconv.FormatUint(sequence.Add(1), 10)
 log.Debugln("[vpn-route] request submitted id=%s target=%s timeout=2s", id, ip)
 selected,err := call(ctx, request{Op:"acquire", ID:id, VPN:vpn, IP:ip.Unmap().String()})
 if err != nil { enqueueRelease(id); return nil, fmt.Errorf("VPN route prepare: %w", err) }
 if selected == "" {enqueueRelease(id);return nil,errors.New("VPN route service did not select an interface")}
 if err = ctx.Err(); err != nil { enqueueRelease(id); return nil, err }
 return &Lease{id:id,Interface:selected}, nil
}
func (l *Lease) Release() { if l != nil { l.once.Do(func(){ enqueueRelease(l.id) }) } }

// Closing sockets must not wait for service I/O. A bounded worker compensates lost replies.
func enqueueRelease(id string) {
 pendingMu.Lock()
 if len(pending) < 32768 { pending[id] = 0 } else { log.Warnln("[vpn-route] release queue full; lease retained until core exit") }
 pendingMu.Unlock()
 select {case wake<-struct{}{}:default:}
 reaper.Do(func(){ go func(){
  ticker := time.NewTicker(time.Second); defer ticker.Stop()
  for {
   select {case <-ticker.C:case <-wake:}
   pendingMu.Lock(); ids := make([]string,0,64)
   for id := range pending { ids=append(ids,id); if len(ids)==64 {break} }; pendingMu.Unlock()
   for _,id := range ids {
    _,err := call(context.Background(), request{Op:"release",ID:id})
    pendingMu.Lock()
    if err == nil { delete(pending,id) } else { pending[id]++; if pending[id]>=3 { delete(pending,id); log.Warnln("[vpn-route] release failed; lease retained until core exit: %v",err) } }
    pendingMu.Unlock()
   }
   pendingMu.Lock();remaining:=len(pending);pendingMu.Unlock()
   if remaining>0 {select {case wake<-struct{}{}:default:}}
  }
 }() })
}

type conn struct { net.Conn; lease *Lease }
func WrapConn(c net.Conn, l *Lease) net.Conn { return &conn{c,l} }
func (c *conn) Close() error { err:=c.Conn.Close(); c.lease.Release(); return err }

type packet struct {
 net.PacketConn
 vpn string
 selected string
 mu sync.Mutex
 closed bool
 leases map[netip.Addr]*Lease
}
func WrapPacket(c net.PacketConn, vpn string, ip netip.Addr, l *Lease) net.PacketConn {
 return &packet{PacketConn:c,vpn:vpn,selected:l.Interface,leases:map[netip.Addr]*Lease{ip.Unmap():l}}
}
func (p *packet) WriteTo(b []byte, addr net.Addr) (int,error) {
 target,err:=netip.ParseAddrPort(addr.String())
 if err!=nil {return 0,errors.New("managed VPN UDP requires resolved target")}
 ip:=target.Addr().Unmap()
 p.mu.Lock()
 if p.closed {p.mu.Unlock();return 0,net.ErrClosed}
 if _,ok:=p.leases[ip];!ok {
  if len(p.leases)>=128 {p.mu.Unlock();return 0,errors.New("managed VPN UDP destination capacity reached")}
  l,err:=Acquire(context.Background(),p.vpn,ip)
  if err!=nil {p.mu.Unlock();return 0,err}
  if l.Interface != p.selected {l.Release();p.mu.Unlock();_ = p.Close();return 0,errors.New("VPN state changed; UDP association closed")}
  p.leases[ip]=l
 }
 p.mu.Unlock()
 return p.PacketConn.WriteTo(b,addr)
}
func (p *packet) Close() error {
 // Interrupt a blocked socket operation before waiting for route bookkeeping.
 err:=p.PacketConn.Close()
 p.mu.Lock(); defer p.mu.Unlock()
 if !p.closed {p.closed=true; for _,l:=range p.leases {l.Release()};p.leases=nil}
 return err
}
