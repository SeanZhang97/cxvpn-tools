package vpnroute

import (
 "context"
 "errors"
 "net"
 "net/netip"
 "os"
 "sync"
 "testing"
 "time"
)
var fakeMu sync.Mutex
var fakeFail bool
var fakeInterface string
var fakeTargets []string
func TestMain(m *testing.M) {
 os.Setenv("CXVPN_ROUTE_EPOCH","offline-test")
 exchange=func(ctx context.Context,r request)(string,error){
  if err:=ctx.Err();err!=nil{return "",err}
  fakeMu.Lock();defer fakeMu.Unlock()
  if r.Op=="acquire" {fakeTargets=append(fakeTargets,r.IP);if fakeFail{return "",errors.New("injected route failure")}}
  if fakeInterface!="" {return fakeInterface,nil};return r.VPN,nil
 }
 os.Exit(m.Run())
}
func resetFake(fail bool){fakeMu.Lock();defer fakeMu.Unlock();fakeFail=fail;fakeTargets=nil;fakeInterface=""}
func TestColdTargetAndCancelledRequest(t *testing.T){
 resetFake(false)
 l,err:=Acquire(context.Background(),"公司 e\u0301 \U0001f1e8\U0001f1f3",netip.MustParseAddr("203.0.113.8"))
 if err!=nil||l==nil{t.Fatal(err)};l.Release();l.Release()
 ctx,cancel:=context.WithCancel(context.Background());cancel()
 if l,err:=Acquire(ctx,"VPN",netip.MustParseAddr("203.0.113.9"));err==nil||l!=nil{t.Fatal("cancelled acquire succeeded")}
}
func TestConcurrentCandidatesHaveDistinctLeases(t *testing.T){
 resetFake(false);var wg sync.WaitGroup;var mu sync.Mutex;ids:=map[string]bool{}
 for i:=0;i<64;i++{wg.Add(1);go func(){defer wg.Done();l,err:=Acquire(context.Background(),"VPN",netip.MustParseAddr("203.0.113.8"));if err!=nil{t.Error(err);return};defer l.Release();mu.Lock();defer mu.Unlock();if ids[l.id]{t.Error("duplicate lease")};ids[l.id]=true}()};wg.Wait()
 if len(ids)!=64{t.Fatal(len(ids))}
}
type fakePacket struct{writes int;closed bool}
func (p *fakePacket)ReadFrom([]byte)(int,net.Addr,error){return 0,nil,net.ErrClosed}
func (p *fakePacket)WriteTo(b []byte,_ net.Addr)(int,error){if p.closed{return 0,net.ErrClosed};p.writes++;return len(b),nil}
func (p *fakePacket)Close()error{p.closed=true;return nil}
func (*fakePacket)LocalAddr()net.Addr{return &net.UDPAddr{}}
func (*fakePacket)SetDeadline(time.Time)error{return nil}
func (*fakePacket)SetReadDeadline(time.Time)error{return nil}
func (*fakePacket)SetWriteDeadline(time.Time)error{return nil}
func TestUDPDestinationChangeIsPreparedBeforeSend(t *testing.T){
 resetFake(false);first:=netip.MustParseAddr("203.0.113.8");l,err:=Acquire(context.Background(),"VPN",first);if err!=nil{t.Fatal(err)}
 raw:=&fakePacket{};p:=WrapPacket(raw,"VPN",first,l);defer p.Close()
 if _,err=p.WriteTo([]byte{1},&net.UDPAddr{IP:net.ParseIP("203.0.113.9"),Port:443});err!=nil{t.Fatal(err)}
 fakeMu.Lock();n:=len(fakeTargets);fakeMu.Unlock();if n!=2||raw.writes!=1{t.Fatal(n,raw.writes)}
 resetFake(true)
 if _,err=p.WriteTo([]byte{1},&net.UDPAddr{IP:net.ParseIP("203.0.113.10"),Port:443});err==nil{t.Fatal("sent before failed route prepare")}
 if raw.writes!=1{t.Fatal("failed target reached socket")}
 p.Close();if _,err=p.WriteTo([]byte{1},&net.UDPAddr{IP:net.ParseIP("203.0.113.8"),Port:443});err==nil{t.Fatal("closed packet reused")}
}

func TestPhysicalSelectionAndUDPStateChange(t *testing.T){
 resetFake(false)
 fakeMu.Lock();fakeInterface="物理 e\u0301 \U0001f1e8\U0001f1f3";fakeMu.Unlock()
 first:=netip.MustParseAddr("203.0.113.8")
 l,err:=Acquire(context.Background(),"VPN",first)
 if err!=nil||l.Interface!="物理 e\u0301 \U0001f1e8\U0001f1f3"{t.Fatal(l,err)}
 raw:=&fakePacket{};p:=WrapPacket(raw,"VPN",first,l);defer p.Close()
 resetFake(false) // VPN is now connected; this socket is still bound to physical.
 if _,err=p.WriteTo([]byte{1},&net.UDPAddr{IP:net.ParseIP("203.0.113.9"),Port:443});err==nil{t.Fatal("state change reused old interface")}
 if raw.writes!=0||!raw.closed{t.Fatal("old UDP association was not closed before sending")}
 fresh,err:=Acquire(context.Background(),"VPN",first);if err!=nil{t.Fatal(err)};defer fresh.Release()
 if fresh.Interface!="VPN"{t.Fatal(fresh.Interface)}
}
