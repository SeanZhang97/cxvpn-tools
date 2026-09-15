package vpnroute

import (
 "context"
 "encoding/binary"
 "encoding/json"
 "errors"
 "io"

 winio "github.com/tailscale/go-winio"
)

func exchangeNative(ctx context.Context, req request) (selected string, resultErr error) {
 c,err:=winio.DialPipeContext(ctx,`\\.\pipe\CXVPNRouteLease.v1`)
 if err!=nil{return "",err};defer c.Close()
 deadline,_:=ctx.Deadline();if err=c.SetDeadline(deadline);err!=nil{return "",err}
 stop:=context.AfterFunc(ctx,func(){_ = c.Close()});defer stop()
 body,err:=json.Marshal(req);if err!=nil{return "",err}
 frame:=make([]byte,4+len(body));binary.LittleEndian.PutUint32(frame,uint32(len(body)));copy(frame[4:],body)
 for len(frame)>0 {n,e:=c.Write(frame);if e!=nil{return "",e};if n==0{return "",io.ErrShortWrite};frame=frame[n:]}
 header:=make([]byte,4);if _,err=io.ReadFull(c,header);err!=nil{return "",err}
 size:=binary.LittleEndian.Uint32(header);if size==0||size>4096{return "",errors.New("invalid route response size")}
 body=make([]byte,size);if _,err=io.ReadFull(c,body);err!=nil{return "",err}
 _,_=c.Write([]byte{1})
 var reply struct{OK bool `json:"ok"`;Error string `json:"error"`; Interface string `json:"interface"`}
 if err=json.Unmarshal(body,&reply);err!=nil{return "",err}
 if !reply.OK{return "",errors.New(reply.Error)}
 return reply.Interface,nil
}
