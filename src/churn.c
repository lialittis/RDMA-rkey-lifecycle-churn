/* Bounded RXE registration-churn experiment harness (rkey-lifecycle).
 *
 * Descriptive characterization only: churns ordinary user MRs on one fixed
 * 4096-byte owner allocation (constant address/length/PD/access), records
 * every registration generation, full 32-bit key, decoded index and low
 * byte, register/deregister results and timestamps into a bounded binary
 * ledger (docs/ledger-schema.md; byte-identical to ledger.py), and samples
 * predeclared recurrence states with nonzero 64-byte RC RDMA WRITE probes
 * at offset 64.  No device reset, index-range restriction, key injection,
 * failed-probe retry, or synthetic recurrence counting.
 *
 * Owner (rdma-server) churns and observes memory.  Requester (rdma-client)
 * keeps the sequenced descriptor history and posts probes.  Control is a
 * versioned TCP stream; the data plane is libibverbs RC (connection
 * sequence checked against rdma-core v50 rc_pingpong).  Original code.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <infiniband/verbs.h>

#define PAGE_BYTES 4096
#define DATA_BYTES 64
#define DATA_OFFSET 64
#define BATCH 4096
#define MR_ACCESS_FLAGS (IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE)
#define CTL_MAGIC UINT64_C(0x434855524E313231)

/* ---------------- SHA-256 (FIPS 180-4) ------------------------------- */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t n; } sha256_ctx;
static const uint32_t K256[64] = {
0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static uint32_t ror32(uint32_t x, int n){ return (x>>n)|(x<<(32-n)); }
static void sha256_init(sha256_ctx *c){
    c->h[0]=0x6a09e667;c->h[1]=0xbb67ae85;c->h[2]=0x3c6ef372;c->h[3]=0xa54ff53a;
    c->h[4]=0x510e527f;c->h[5]=0x9b05688c;c->h[6]=0x1f83d9ab;c->h[7]=0x5be0cd19;
    c->len=0;c->n=0; }
static void sha256_block(sha256_ctx *c, const uint8_t p[64]){
    uint32_t w[64], v[8], t1, t2; int i;
    for(i=0;i<16;i++) w[i]=((uint32_t)p[4*i]<<24)|((uint32_t)p[4*i+1]<<16)|((uint32_t)p[4*i+2]<<8)|(uint32_t)p[4*i+3];
    for(;i<64;i++){
        uint32_t s0=ror32(w[i-15],7)^ror32(w[i-15],18)^(w[i-15]>>3);
        uint32_t s1=ror32(w[i-2],17)^ror32(w[i-2],19)^(w[i-2]>>10);
        w[i]=w[i-16]+s0+w[i-7]+s1; }
    for(i=0;i<8;i++) v[i]=c->h[i];
    for(i=0;i<64;i++){
        uint32_t S1=ror32(v[4],6)^ror32(v[4],11)^ror32(v[4],25);
        uint32_t ch=(v[4]&v[5])^((~v[4])&v[6]);
        t1=v[7]+S1+ch+K256[i]+w[i];
        uint32_t S0=ror32(v[0],2)^ror32(v[0],13)^ror32(v[0],22);
        uint32_t maj=(v[0]&v[1])^(v[0]&v[2])^(v[1]&v[2]);
        t2=S0+maj;
        v[7]=v[6];v[6]=v[5];v[5]=v[4];v[4]=v[3]+t1;v[3]=v[2];v[2]=v[1];v[1]=v[0];v[0]=t1+t2; }
    for(i=0;i<8;i++) c->h[i]+=v[i]; }
static void sha256_update(sha256_ctx *c, const void *data, size_t n){
    const uint8_t *p=data; c->len+=n;
    while(n){ size_t k=64-c->n; if(k>n)k=n; memcpy(c->buf+c->n,p,k); c->n+=k; p+=k; n-=k;
        if(c->n==64){ sha256_block(c,c->buf); c->n=0; } } }
static void sha256_final(sha256_ctx *c, uint8_t out[32]){
    uint64_t bits=htobe64(c->len*8); uint8_t pad=0x80, zero=0;
    sha256_update(c,&pad,1);
    while(c->n!=56) sha256_update(c,&zero,1);
    sha256_update(c,&bits,8);
    for(int i=0;i<8;i++){ out[4*i]=(uint8_t)(c->h[i]>>24); out[4*i+1]=(uint8_t)(c->h[i]>>16);
        out[4*i+2]=(uint8_t)(c->h[i]>>8); out[4*i+3]=(uint8_t)c->h[i]; } }
static void sha256_buf(const void *data, size_t n, uint8_t out[32]){
    sha256_ctx c; sha256_init(&c); sha256_update(&c,data,n); sha256_final(&c,out); }
static void sha_hex(const uint8_t d[32], char out[65]){
    static const char hx[]="0123456789abcdef";
    for(int i=0;i<32;i++){ out[2*i]=hx[d[i]>>4]; out[2*i+1]=hx[d[i]&15]; } out[64]=0; }

/* ---------------- binary ledger writer (mirror of ledger.py) --------- */
#define LG_REG 1
#define LG_DEREG 2
#define LG_CONTROL 3
#define LG_PROBE 4
#define LG_META 5
#define LG_CHECKPOINT 254
#define LG_TRAILER 255
#define REC_REG 92
#define REC_DEREG 24
#define REC_CONTROL 36
#define REC_PROBE 316
#define REC_META 80
#define REC_CHAIN 16

typedef struct { FILE *f; uint8_t chain[8]; uint32_t count, since_ckpt; } ledger_writer;

static void sha8(const void *data, size_t n, uint8_t out[8]){
    uint8_t full[32]; sha256_buf(data,n,full); memcpy(out,full,8); }

static void lg_frame(ledger_writer *w, uint8_t kind, const void *payload, uint32_t plen){
    uint8_t *kdata=malloc((size_t)plen+1);
    if(!kdata){ fprintf(stderr,"oom\n"); exit(3); }
    kdata[0]=kind; memcpy(kdata+1,payload,plen);
    uint8_t ck[8]; sha8(kdata,(size_t)plen+1,ck); free(kdata);
    uint16_t len16=(uint16_t)plen;
    if(fwrite(&len16,2,1,w->f)!=1||fwrite(&kind,1,1,w->f)!=1
       ||fwrite(payload,plen,1,w->f)!=1||fwrite(ck,8,1,w->f)!=8/8){
        fprintf(stderr,"ledger write failure\n"); exit(3); }
    uint8_t step_in[16], step_out[32];
    memcpy(step_in,w->chain,8); memcpy(step_in+8,ck,8);
    sha256_buf(step_in,16,step_out);
    memcpy(w->chain,step_out,8);
    w->count++; w->since_ckpt++; }

static uint64_t lg_chain_u64(const ledger_writer *w){
    uint64_t v=0;
    for(int i=7;i>=0;i--) v=(v<<8)|w->chain[i];   /* little-endian bytes */
    return v; }

static void lg_checkpoint(ledger_writer *w){
    uint8_t p[REC_CHAIN]; uint64_t ch=lg_chain_u64(w); uint32_t cnt=w->count, pad=0;
    memcpy(p,&ch,8); memcpy(p+8,&cnt,4); memcpy(p+12,&pad,4);
    lg_frame(w,LG_CHECKPOINT,p,REC_CHAIN); w->since_ckpt=0; }

static void lg_append(ledger_writer *w, uint8_t kind, const void *payload, uint32_t plen){
    lg_frame(w,kind,payload,plen);
    if(w->since_ckpt>=BATCH) lg_checkpoint(w); }

static void lg_meta(ledger_writer *w, int requester_side, const char *label,
                    const char *role, const char *stop, int outlived, uint64_t mono){
    uint8_t p[REC_META]; memset(p,0,sizeof p);
    p[0]=(uint8_t)requester_side;
    snprintf((char*)p+1,32,"%s",label);
    snprintf((char*)p+32,9,"%s",role);
    snprintf((char*)p+40,25,"%s",stop?stop:"");
    p[64]=(uint8_t)(outlived<0?2:(outlived?1:0));
    memcpy(p+72,&mono,8);
    lg_append(w,LG_META,p,REC_META); }

static void lg_registration(ledger_writer *w, uint32_t gen, uint32_t rkey,
                            uint64_t addr, uint32_t len, uint64_t pd,
                            const char *access, int32_t rc, uint32_t adv,
                            uint64_t mono, int target){
    uint8_t p[REC_REG]; memset(p,0,sizeof p);
    uint32_t idx=rkey>>8, low=rkey&0xFFu;
    memcpy(p+0,&gen,4); memcpy(p+4,&rkey,4); memcpy(p+8,&idx,4); memcpy(p+12,&low,4);
    memcpy(p+16,&addr,8); memcpy(p+24,&len,4); memcpy(p+28,&pd,8);
    snprintf((char*)p+36,33,"%s",access);
    memcpy(p+68,&rc,4); memcpy(p+72,&adv,4); memcpy(p+76,&mono,8);
    p[84]=(uint8_t)(target?1:0);
    lg_append(w,LG_REG,p,REC_REG); }

static void lg_dereg(ledger_writer *w, uint32_t gen, int32_t rc, uint32_t quiesce, uint64_t mono){
    uint8_t p[REC_DEREG]; memset(p,0,sizeof p);
    memcpy(p+0,&gen,4); memcpy(p+4,&rc,4); memcpy(p+8,&quiesce,4); memcpy(p+12,&mono,8);
    lg_append(w,LG_DEREG,p,REC_DEREG); }

static void lg_control(ledger_writer *w, uint32_t seq, uint32_t code,
                       uint32_t lo, uint32_t hi, int acked, int requester_side,
                       uint64_t mono){
    uint8_t p[REC_CONTROL]; memset(p,0,sizeof p);
    memcpy(p+0,&seq,4); memcpy(p+4,&code,4); memcpy(p+8,&lo,4); memcpy(p+12,&hi,4);
    p[16]=(uint8_t)(acked?1:0); p[17]=(uint8_t)requester_side;
    memcpy(p+20,&mono,8);
    lg_append(w,LG_CONTROL,p,REC_CONTROL); }

typedef struct {
    char tag[24]; uint32_t gen; uint8_t phase; int32_t posting_rc; int32_t wc;
    int timed_out; uint8_t qp_role; int32_t qp_state; int retired;
    uint32_t rkey; uint64_t addr; uint32_t len; uint64_t pd; const char *access;
    const char *sha_before, *sha_after, *sha_expected;
    int guards; /* 1 intact, 0 broken, -1 unknown */
    uint64_t mono;
} probe_log;

static void lg_probe(ledger_writer *w, const probe_log *pl){
    uint8_t p[REC_PROBE]; memset(p,0,sizeof p);
    snprintf((char*)p+0,25,"%s",pl->tag);
    memcpy(p+24,&pl->gen,4);
    p[28]=pl->phase;
    memcpy(p+32,&pl->posting_rc,4); memcpy(p+36,&pl->wc,4);
    p[40]=(uint8_t)(pl->timed_out?1:0); p[41]=pl->qp_role;
    memcpy(p+42,&pl->qp_state,4); p[46]=(uint8_t)(pl->retired?1:0);
    memcpy(p+48,&pl->rkey,4); memcpy(p+52,&pl->addr,8);
    memcpy(p+60,&pl->len,4); memcpy(p+64,&pl->pd,8);
    snprintf((char*)p+72,33,"%s",pl->access?pl->access:"");
    if(pl->sha_before) memcpy(p+108,pl->sha_before,strlen(pl->sha_before));
    if(pl->sha_after)  memcpy(p+172,pl->sha_after,strlen(pl->sha_after));
    if(pl->sha_expected) memcpy(p+236,pl->sha_expected,strlen(pl->sha_expected));
    p[300]=(uint8_t)(pl->guards<0?2:(pl->guards?1:0));
    memcpy(p+308,&pl->mono,8);
    lg_append(w,LG_PROBE,p,REC_PROBE); }

static int lg_open(ledger_writer *w, const char *path){
    memset(w,0,sizeof *w);
    w->f=fopen(path,"wb");
    if(!w->f) return -1;
    uint8_t hdr[16]; memcpy(hdr,"RKEYCHN1",8);
    uint32_t ver=1, flags=0; memcpy(hdr+8,&ver,4); memcpy(hdr+12,&flags,4);
    if(fwrite(hdr,16,1,w->f)!=1) return -1;
    sha8(hdr,16,w->chain);
    return 0; }

static void lg_close(ledger_writer *w){
    if(w->since_ckpt) lg_checkpoint(w);
    uint8_t p[REC_CHAIN]; uint64_t ch=lg_chain_u64(w); uint32_t cnt=w->count, pad=0;
    memcpy(p,&ch,8); memcpy(p+8,&cnt,4); memcpy(p+12,&pad,4);
    lg_frame(w,LG_TRAILER,p,REC_CHAIN);
    fflush(w->f); fsync(fileno(w->f)); fclose(w->f); w->f=NULL; }

/* ---------------- control channel ------------------------------------ */
enum { M_HELLO=1, M_QP_CONNECT=2, M_QP_DESTROY=3, M_DESC_BATCH=4,
       M_BATCH_ACK=5, M_QUIESCE=6, M_PROBE_REQ=7, M_PROBE_RESULT=8,
       M_SAMPLE_BEGIN=9, M_RUN_END=10, M_ERROR=11 };
enum { C_RUN_READY=1, C_DESC_BATCH=2, C_BATCH_ACK=3, C_QUIESCE=4,
       C_PROBE_REQ=5, C_PROBE_RESULT=6, C_SAMPLE_BEGIN=7, C_RUN_END=8,
       C_DESC_RECEIVED=9 };
enum { P_POSCTL=1, P_INDEX_ONLY=2, P_FULLKEY_LIVE=3, P_FULLKEY_POST=4,
       P_SELFTEST_LIVE=5, P_SELFTEST_POST=6 };
enum { SLOT_CTL=0, SLOT_A=1, SLOT_B=2 };
enum { PROBE_KIND_CURRENT=1, PROBE_KIND_SAVED=2 };
enum { EXIT_OK=0, EXIT_CONTROL=3, EXIT_MEMORY=4, EXIT_CLEANUP=5,
       EXIT_PROBE_TIMEOUT=6, EXIT_EXPLICIT=7 };

struct message { uint64_t magic, seq, kind, v1, v2, v3, v4; };

static int sockfd=-1, is_requester;
static uint64_t txseq, rxseq;
static uint64_t io_budget_ns=UINT64_C(5000000000);
static volatile sig_atomic_t stop_flag;
static ledger_writer LG;
static uint32_t ctl_seq;
static uint64_t first_reg_ns;
static const char *run_role="pilot", *run_label="run";

static uint64_t now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW,&t);
    return (uint64_t)t.tv_sec*1000000000ull+(uint64_t)t.tv_nsec; }

static void logline(const char *fmt, ...){
    va_list ap; va_start(ap,fmt);
    printf("{\"role\":\"%s\",\"mono_ns\":%llu,\"msg\":\"",
           is_requester?"requester":"owner",
           (unsigned long long)now_ns());
    vprintf(fmt,ap);
    puts("\"}"); fflush(stdout); va_end(ap); }

static void (*abort_hook)(int, const char*) = NULL;
static void die_control(const char *what){
    logline("fatal control failure: %s errno=%d",what,errno);
    if(sockfd>=0){ struct message m; memset(&m,0,sizeof m);
        m.magic=htobe64(CTL_MAGIC); m.seq=htobe64(++txseq); m.kind=htobe64(M_ERROR);
        m.v1=htobe64(EXIT_CONTROL);
        ssize_t n=send(sockfd,&m,sizeof m,MSG_NOSIGNAL); (void)n; }
    if(abort_hook) abort_hook(EXIT_CONTROL,"control-failure");
    exit(EXIT_CONTROL); }
static void require(int ok, const char *what){ if(!ok) die_control(what); }

static void io_all(void *buf, size_t count, int writing){
    size_t done=0; uint64_t limit=now_ns()+io_budget_ns;
    while(done<count){
        int ms=(int)((limit>now_ns()?limit-now_ns():0)/1000000);
        struct pollfd f={.fd=sockfd,.events=writing?POLLOUT:POLLIN};
        int p=poll(&f,1,ms);
        if(p<0 && errno==EINTR) continue;
        if(p<=0) die_control("TCP deadline");
        ssize_t n=writing?send(sockfd,(char*)buf+done,count-done,MSG_NOSIGNAL)
                         :recv(sockfd,(char*)buf+done,count-done,0);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) die_control("TCP transfer");
        done+=(size_t)n; } }

static void send_msg(uint64_t kind, uint64_t v1, uint64_t v2, uint64_t v3, uint64_t v4){
    struct message m; memset(&m,0,sizeof m);
    m.magic=htobe64(CTL_MAGIC); m.seq=htobe64(++txseq); m.kind=htobe64(kind);
    m.v1=htobe64(v1); m.v2=htobe64(v2); m.v3=htobe64(v3); m.v4=htobe64(v4);
    io_all(&m,sizeof m,1); }

static struct message recv_msg(uint64_t expect_kind){
    struct message m;
    if(expect_kind==M_PROBE_RESULT) io_budget_ns=UINT64_C(10000000000);
    io_all(&m,sizeof m,0);
    io_budget_ns=UINT64_C(5000000000);
    m.magic=be64toh(m.magic); m.seq=be64toh(m.seq); m.kind=be64toh(m.kind);
    m.v1=be64toh(m.v1); m.v2=be64toh(m.v2); m.v3=be64toh(m.v3); m.v4=be64toh(m.v4);
    require(m.magic==CTL_MAGIC && m.seq==++rxseq,"control identity/sequence");
    if(expect_kind && m.kind!=expect_kind) die_control("unexpected control kind");
    return m; }

static void connect_tcp(int port, const char *local_ip, const char *peer_ip){
    int s=socket(AF_INET,SOCK_STREAM,0); require(s>=0,"socket");
    int one=1; require(!setsockopt(s,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one),"SO_REUSEADDR");
    struct sockaddr_in local={.sin_family=AF_INET};
    require(inet_pton(AF_INET,local_ip,&local.sin_addr)==1,"local IP");
    local.sin_port=is_requester?0:htons((uint16_t)port);
    require(!bind(s,(struct sockaddr*)&local,sizeof local),"bind TCP");
    if(!is_requester){
        require(!listen(s,1),"listen");
        struct pollfd f={s,POLLIN,0};
        if(poll(&f,1,10000)<=0) die_control("accept deadline");
        sockfd=accept(s,NULL,NULL); require(sockfd>=0,"accept"); close(s);
    } else {
        struct sockaddr_in peer={.sin_family=AF_INET,.sin_port=htons((uint16_t)port)};
        require(inet_pton(AF_INET,peer_ip,&peer.sin_addr)==1,"peer IP");
        int ok=-1;
        for(int attempt=0;attempt<120 && ok<0;attempt++){
            ok=connect(s,(struct sockaddr*)&peer,sizeof peer);
            if(ok<0){ struct timespec ts={0,250000000}; nanosleep(&ts,NULL); } }
        if(ok<0) die_control("connect TCP");
        sockfd=s; } }

/* ---------------- RDMA resources ------------------------------------- */
static struct ibv_context *ctx;
static struct ibv_pd *pd;
static struct ibv_port_attr port_attr;
static union ibv_gid gid;
static int gid_index=-1;
static unsigned char *buffer;
static unsigned char guard_master[PAGE_BYTES];
static uint64_t peer_base_addr;
static uint32_t src_lkey;

struct qp_endpoint { struct ibv_qp *qp; struct ibv_cq *cq; uint32_t psn; int used; };

static void setup_device(const char *local_ip, int need_buffer){
    int count; struct ibv_device **list=ibv_get_device_list(&count);
    require(list!=NULL,"device list");
    ctx=NULL;
    for(int i=0;i<count;i++) if(!strcmp(ibv_get_device_name(list[i]),"rxe0")) ctx=ibv_open_device(list[i]);
    ibv_free_device_list(list); require(ctx!=NULL,"open rxe0");
    require(!ibv_query_port(ctx,1,&port_attr),"query port");
    require(port_attr.state==IBV_PORT_ACTIVE,"port active");
    struct in_addr ip; require(inet_pton(AF_INET,local_ip,&ip)==1,"GID IP");
    static const unsigned char mapped[12]={0,0,0,0,0,0,0,0,0,0,255,255};
    for(int i=0;i<port_attr.gid_tbl_len;i++){
        union ibv_gid cand;
        if(!ibv_query_gid(ctx,1,i,&cand) && !memcmp(cand.raw,mapped,12)
           && !memcmp(cand.raw+12,&ip,4)){ gid=cand; gid_index=i; break; } }
    require(gid_index>=0,"IPv4 mapped GID");
    pd=ibv_alloc_pd(ctx); require(pd!=NULL,"alloc PD");
    if(need_buffer){
        if(posix_memalign((void**)&buffer,PAGE_BYTES,PAGE_BYTES)) die_control("alloc buffer");
        for(int i=0;i<PAGE_BYTES;i++) buffer[i]=(unsigned char)(0xA5u^(unsigned)(i*13));
        memcpy(guard_master,buffer,PAGE_BYTES); } }

static void create_qp(struct qp_endpoint *e){
    e->cq=ibv_create_cq(ctx,16,NULL,NULL,0); require(e->cq!=NULL,"create CQ");
    struct ibv_qp_init_attr init;
    memset(&init,0,sizeof init);
    init.send_cq=e->cq; init.recv_cq=e->cq; init.qp_type=IBV_QPT_RC;
    init.cap.max_send_wr=16; init.cap.max_recv_wr=16;
    init.cap.max_send_sge=1; init.cap.max_recv_sge=1;
    e->qp=ibv_create_qp(pd,&init); require(e->qp!=NULL,"create QP");
    e->psn=(uint32_t)(now_ns()&0xFFFFFFu);
    struct ibv_qp_attr attr; memset(&attr,0,sizeof attr);
    attr.qp_state=IBV_QPS_INIT; attr.pkey_index=0; attr.port_num=1;
    attr.qp_access_flags=MR_ACCESS_FLAGS;
    require(!ibv_modify_qp(e->qp,&attr,IBV_QP_STATE|IBV_QP_PKEY_INDEX|
        IBV_QP_PORT|IBV_QP_ACCESS_FLAGS),"QP INIT"); }

static void qp_to_rtr_rts(struct qp_endpoint *e, uint32_t peer_qpn,
                          uint32_t peer_psn, const union ibv_gid *peer_gid){
    struct ibv_ah_attr ah; memset(&ah,0,sizeof ah);
    ah.is_global=1; ah.port_num=1; ah.grh.dgid=*peer_gid;
    ah.grh.sgid_index=(uint8_t)gid_index; ah.grh.hop_limit=1;
    struct ibv_qp_attr attr; memset(&attr,0,sizeof attr);
    attr.qp_state=IBV_QPS_RTR; attr.path_mtu=IBV_MTU_1024;
    attr.dest_qp_num=peer_qpn; attr.rq_psn=peer_psn;
    attr.max_dest_rd_atomic=1; attr.min_rnr_timer=12; attr.ah_attr=ah;
    require(!ibv_modify_qp(e->qp,&attr,IBV_QP_STATE|IBV_QP_AV|IBV_QP_PATH_MTU|
        IBV_QP_DEST_QPN|IBV_QP_RQ_PSN|IBV_QP_MAX_DEST_RD_ATOMIC|
        IBV_QP_MIN_RNR_TIMER),"QP RTR");
    memset(&attr,0,sizeof attr);
    attr.qp_state=IBV_QPS_RTS; attr.sq_psn=e->psn;
    attr.timeout=8; attr.retry_cnt=3; attr.rnr_retry=3; attr.max_rd_atomic=1;
    require(!ibv_modify_qp(e->qp,&attr,IBV_QP_STATE|IBV_QP_SQ_PSN|
        IBV_QP_TIMEOUT|IBV_QP_RETRY_CNT|IBV_QP_RNR_RETRY|
        IBV_QP_MAX_QP_RD_ATOMIC),"QP RTS"); }

static void qp_payload(uint64_t out[6], struct qp_endpoint *e){
    out[0]=htobe64(e->qp->qp_num); out[1]=htobe64(e->psn);
    memcpy(out+2,gid.raw,16); }

static void qp_connect_peer(struct qp_endpoint *e, const uint64_t peer[6]){
    uint32_t peer_qpn=(uint32_t)be64toh(peer[0]);
    uint32_t peer_psn=(uint32_t)be64toh(peer[1]);
    union ibv_gid peer_gid; memcpy(peer_gid.raw,peer+2,16);
    qp_to_rtr_rts(e,peer_qpn,peer_psn,&peer_gid); }

/* owner side: send header+payload, then read the requester's reply */
static void exchange_qp(struct qp_endpoint *e, uint64_t slot){
    create_qp(e);
    uint64_t mine[6], peer[6];
    qp_payload(mine,e);
    send_msg(M_QP_CONNECT,slot,0,0,0);
    io_all(mine,sizeof mine,1);
    recv_msg(M_QP_CONNECT);
    io_all(peer,sizeof peer,0);
    qp_connect_peer(e,peer);
    send_msg(M_QUIESCE,slot,0,0,0);
    recv_msg(M_QUIESCE);
    logline("qp connected slot=%llu qpn=%u",(unsigned long long)slot,e->qp->qp_num); }

/* requester side: header already consumed by the message loop */
static void exchange_qp_reply(struct qp_endpoint *e, uint64_t slot){
    create_qp(e);
    uint64_t mine[6], peer[6];
    io_all(peer,sizeof peer,0);
    qp_payload(mine,e);
    send_msg(M_QP_CONNECT,slot,0,0,0);
    io_all(mine,sizeof mine,1);
    qp_connect_peer(e,peer);
    recv_msg(M_QUIESCE);
    send_msg(M_QUIESCE,0,0,0,0);
    logline("qp connected slot=%llu qpn=%u",(unsigned long long)slot,e->qp->qp_num); }

static void destroy_qp_pair(struct qp_endpoint *e, uint64_t slot){
    if(e->qp){ ibv_destroy_qp(e->qp); e->qp=NULL;
        logline("qp destroyed slot=%llu",(unsigned long long)slot); }
    if(e->cq){ ibv_destroy_cq(e->cq); e->cq=NULL; }
    e->used=0; }

static int query_qp_state(struct qp_endpoint *e){
    if(!e->qp) return -1;
    struct ibv_qp_attr a; struct ibv_qp_init_attr init;
    memset(&a,0,sizeof a); memset(&init,0,sizeof init);
    int r=ibv_query_qp(e->qp,&a,IBV_QP_STATE,&init);
    return r?-1:(int)a.qp_state; }

typedef struct { int posting_rc; int wc_status; int timed_out; } write_result;

/* one 64B signaled RDMA WRITE; no retry is ever attempted */
static write_result post_write(struct qp_endpoint *e, uint64_t remote_addr,
                               uint32_t rkey, const void *payload, uint64_t wr_id){
    write_result r={0,-1,0};
    if(!e->qp){ r.posting_rc=EINVAL; return r; }
    struct ibv_sge sge;
    sge.addr=(uintptr_t)payload; sge.length=DATA_BYTES; sge.lkey=src_lkey;
    struct ibv_send_wr wr, *bad=NULL;
    memset(&wr,0,sizeof wr);
    wr.wr_id=wr_id; wr.opcode=IBV_WR_RDMA_WRITE; wr.sg_list=&sge; wr.num_sge=1;
    wr.send_flags=IBV_SEND_SIGNALED;
    wr.wr.rdma.remote_addr=remote_addr; wr.wr.rdma.rkey=rkey;
    r.posting_rc=ibv_post_send(e->qp,&wr,&bad);
    if(r.posting_rc) return r;
    struct ibv_wc wc; int done=0; uint64_t limit=now_ns()+UINT64_C(5000000000);
    while(now_ns()<limit){
        int n=ibv_poll_cq(e->cq,1,&wc);
        if(n<0){ r.posting_rc=EIO; return r; }
        if(n==1){ done=1; r.wc_status=(int)wc.status; break; } }
    if(!done) r.timed_out=1;
    return r; }

/* distinct nonzero payloads/sentinels from (gen, tag, salt) */
static void fill_pattern(unsigned char *out, uint32_t gen, uint32_t tag, uint32_t salt){
    for(int i=0;i<DATA_BYTES;i++){
        unsigned v=(gen*2654435761u)^(tag*40503u)^(salt*97u)^(unsigned)(i*7);
        out[i]=(unsigned char)((v&0xFFu)|1u); } }

static void sig_handler(int sig){ (void)sig; stop_flag=1; }

/* ================= shared helpers ==================================== */
static const char *access_str="3"; /* actual ibv_access_flags value */

/* ================= OWNER ============================================= */
static uint32_t *index_first_tab;   /* index -> first generation (0 none) */
static uint32_t *rkey_hash_keys, *rkey_hash_gens;  /* open addressing */
static uint64_t rkey_hash_mask;
static struct qp_endpoint q_ctl, q_a, q_b;
static struct ibv_mr *cur_mr;
static uint32_t cur_gen, cur_rkey;
static uint32_t io_samples, fk_samples, probe_counter;
static uint32_t batch_keys[BATCH];
static uint32_t batch_fill, batch_lo=1;
static uint64_t deadline_abs_ns;
static int probe_codes;

static int rkey_hash_lookup(uint32_t rkey, uint32_t *gen_out){
    uint64_t h=((uint64_t)rkey*11400714819323198485ull)&rkey_hash_mask;
    for(uint64_t probe=0;probe<=rkey_hash_mask;probe++){
        uint64_t i=(h+probe)&rkey_hash_mask;
        if(rkey_hash_gens[i]==0) return 0;
        if(rkey_hash_keys[i]==rkey){ *gen_out=rkey_hash_gens[i]; return 1; } }
    return 0; }

static void rkey_hash_insert(uint32_t rkey, uint32_t gen){
    uint64_t h=((uint64_t)rkey*11400714819323198485ull)&rkey_hash_mask;
    for(uint64_t probe=0;;probe++){
        uint64_t i=(h+probe)&rkey_hash_mask;
        if(rkey_hash_gens[i]==0){ rkey_hash_gens[i]=gen; rkey_hash_keys[i]=rkey; return; }
        if(rkey_hash_keys[i]==rkey) return; } }

static void owner_send_batch(void){
    if(batch_fill==0) return;
    uint32_t keys[BATCH];
    for(uint32_t i=0;i<batch_fill;i++) keys[i]=htobe32(batch_keys[i]);
    send_msg(M_DESC_BATCH,batch_lo,batch_fill,0,0);
    lg_control(&LG,++ctl_seq,C_DESC_BATCH,batch_lo,batch_lo+batch_fill-1,0,0,now_ns());
    io_all(keys,batch_fill*4,1);
    recv_msg(M_BATCH_ACK);
    lg_control(&LG,++ctl_seq,C_BATCH_ACK,batch_lo,batch_lo+batch_fill-1,1,0,now_ns());
    batch_lo+=batch_fill; batch_fill=0; }

static uint32_t quiesce_local(void){
    uint32_t s=++ctl_seq;
    lg_control(&LG,s,C_QUIESCE,0,0,1,0,now_ns());
    return s; }

static uint32_t quiesce_barrier(void){
    send_msg(M_QUIESCE,0,0,0,0);
    recv_msg(M_QUIESCE);
    uint32_t s=++ctl_seq;
    lg_control(&LG,s,C_QUIESCE,0,0,1,0,now_ns());
    return s; }

static int guards_intact(void){
    if(memcmp(buffer,guard_master,DATA_OFFSET)) return 0;
    if(memcmp(buffer+DATA_OFFSET+DATA_BYTES,
              guard_master+DATA_OFFSET+DATA_BYTES,
              PAGE_BYTES-DATA_OFFSET-DATA_BYTES)) return 0;
    return 1; }
static void take_sha(char out[65]){
    uint8_t d[32]; sha256_buf(buffer,PAGE_BYTES,d); sha_hex(d,out); }

typedef struct { write_result wr; int guards; char before[65], after[65], expected[65]; } probe_obs;

/* Ask the requester to WRITE 64B at offset 64 through slot's QP, using
 * either the current rkey (kind 1, rkey in rk) or its saved descriptor
 * for generation rk (kind 2).  Owner prepares sentinel/payload and
 * snapshots the allocation. */
static char snap_expect_buf[65];
static probe_obs owner_request_probe(uint64_t slot, int kind, uint32_t rk,
                                     uint32_t payload_gen, uint32_t tag){
    probe_obs o;
    unsigned char payload[DATA_BYTES], sentinel[DATA_BYTES];
    fill_pattern(payload,payload_gen,tag,1);
    fill_pattern(sentinel,payload_gen,0xFEEDu,2);
    memcpy(buffer+DATA_OFFSET,payload,DATA_BYTES); take_sha(snap_expect_buf);
    memcpy(buffer+DATA_OFFSET,sentinel,DATA_BYTES); take_sha(o.before);
    snprintf(o.expected,65,"%s",snap_expect_buf);
    send_msg(M_PROBE_REQ,(uint64_t)kind,(uint64_t)rk,slot,
             ((uint64_t)payload_gen<<32)|(uint64_t)tag);
    lg_control(&LG,++ctl_seq,C_PROBE_REQ,(uint32_t)kind,rk,
               (uint32_t)slot,0,now_ns());
    struct message res=recv_msg(M_PROBE_RESULT);
    logline("probe result posting=%d wc=%d timed_out=%llu guards=%d",
            (int)(int64_t)res.v1,(int)(int64_t)res.v2,
            (unsigned long long)res.v3,guards_intact());
    lg_control(&LG,++ctl_seq,C_PROBE_RESULT,(uint32_t)res.v1,(uint32_t)res.v2,
               (uint32_t)res.v3,1,now_ns());
    take_sha(o.after);
    o.wr.posting_rc=(int32_t)res.v1; o.wr.wc_status=(int32_t)res.v2;
    o.wr.timed_out=(int)(res.v3!=0);
    o.guards=guards_intact();
    return o; }

static void owner_log_probe(const char *tag, uint32_t gen, uint8_t phase,
                            uint8_t qp_role, struct qp_endpoint *e, int retired,
                            uint32_t rkey, const probe_obs *o, int expect_write){
    probe_log pl;
    memset(&pl,0,sizeof pl);
    snprintf(pl.tag,sizeof pl.tag,"%s",tag);
    pl.gen=gen; pl.phase=phase;
    pl.posting_rc=o->wr.posting_rc; pl.wc=o->wr.wc_status;
    pl.timed_out=o->wr.timed_out;
    pl.qp_role=qp_role; pl.qp_state=query_qp_state(e);
    pl.retired=retired; pl.rkey=rkey;
    pl.addr=(uintptr_t)buffer; pl.len=PAGE_BYTES; pl.pd=(uintptr_t)pd;
    pl.access=access_str;
    pl.sha_before=o->before; pl.sha_after=o->after;
    pl.sha_expected=expect_write?o->expected:o->before;
    pl.guards=o->guards; pl.mono=now_ns();
    lg_probe(&LG,&pl);
    probe_codes++; }

static int obs_accepted(const probe_obs *o){
    return o->wr.posting_rc==0 && o->wr.wc_status==0 && o->guards
           && strcmp(o->after,o->expected)==0; }
static int obs_rejected(const probe_obs *o){
    return o->wr.posting_rc==0 && o->wr.wc_status==10 && o->guards
           && strcmp(o->after,o->before)==0; }

static uint32_t owner_register_gen(void){
    uint32_t gen=++cur_gen;
    cur_mr=ibv_reg_mr(pd,buffer,PAGE_BYTES,MR_ACCESS_FLAGS);
    lg_registration(&LG,gen,cur_mr?(uint32_t)cur_mr->rkey:0,
                    (uintptr_t)buffer,PAGE_BYTES,(uintptr_t)pd,access_str,
                    cur_mr?0:errno,gen,now_ns(),1);
    if(!cur_mr){ logline("register failed gen=%u errno=%d",gen,errno);
        if(abort_hook) abort_hook(EXIT_CONTROL,"control-failure");
        exit(EXIT_CONTROL); }
    cur_rkey=(uint32_t)cur_mr->rkey;
    if(first_reg_ns==0) first_reg_ns=now_ns();
    batch_keys[batch_fill++]=cur_rkey;
    return gen; }

static void owner_note_recurrence(void){
    uint32_t idx=cur_rkey>>8;
    if(!index_first_tab[idx]) index_first_tab[idx]=cur_gen;
    rkey_hash_insert(cur_rkey,cur_gen); }

static uint32_t owner_dereg_gen(uint32_t quiesce_seq){
    uint32_t gen=cur_gen;
    int rc=cur_mr?ibv_dereg_mr(cur_mr):-1;
    if(cur_mr && rc){ logline("dereg failed gen=%u rc=%d",gen,rc);
        lg_dereg(&LG,gen,rc,quiesce_seq,now_ns());
        if(abort_hook) abort_hook(EXIT_CLEANUP,"cleanup-warning");
        exit(EXIT_CLEANUP); }
    lg_dereg(&LG,gen,0,quiesce_seq,now_ns());
    cur_mr=NULL;
    return gen; }

/* positive control through the never-failing control QP; run stops on
 * anything but an accepted nonzero write */
static probe_obs positive_control(uint32_t gen, const char *tag){
    probe_obs o=owner_request_probe(SLOT_CTL,PROBE_KIND_CURRENT,cur_rkey,
                                    gen,++probe_counter);
    owner_log_probe(tag,gen,P_POSCTL,0,&q_ctl,0,cur_rkey,&o,1);
    if(!obs_accepted(&o)){
        logline("positive control failed gen=%u posting=%d wc=%d guards=%d",
                gen,o.wr.posting_rc,o.wr.wc_status,o.guards);
        if(abort_hook) abort_hook(EXIT_CONTROL,"control-failure");
        exit(EXIT_CONTROL); }
    return o; }

static void owner_teardown(const char *reason, int code){
    if(cur_mr){ quiesce_local(); owner_dereg_gen(ctl_seq); }
    destroy_qp_pair(&q_ctl,SLOT_CTL);
    destroy_qp_pair(&q_a,SLOT_A);
    destroy_qp_pair(&q_b,SLOT_B);
    /* the fixed allocation is still alive here: all QPs are drained and
     * destroyed before it is released */
    lg_meta(&LG,0,run_label,run_role,reason,1,now_ns());
    lg_close(&LG);
    if(sockfd>=0){ close(sockfd); sockfd=-1; }
    logline("run finished reason=%s code=%d",reason,code);
    if(pd){ ibv_dealloc_pd(pd); pd=NULL; }
    if(buffer){ free(buffer); buffer=NULL; } }

static void owner_finish(const char *reason, int code){
    owner_send_batch();
    send_msg(M_RUN_END,0,0,0,0);
    recv_msg(M_RUN_END);
    lg_control(&LG,++ctl_seq,C_RUN_END,0,0,1,0,now_ns());
    owner_teardown(reason,code);
    exit(code); }

/* best-effort abort: peer may be gone; still close the ledger cleanly */
static void owner_abort(int code, const char *reason){
    if(sockfd>=0){
        send_msg(M_RUN_END,0,0,0,0);
        send_msg(M_ERROR,(uint64_t)code,0,0,0); }
    owner_teardown(reason,code);
    exit(code); }

static int owner_main(int argc, char **argv){
    /* churn owner LOCAL PEER PORT ROLE LABEL MAXREGS DEADLINE_S LEDGER [selftest] */
    const char *local_ip=argv[2], *peer_ip=argv[3];
    int port=(int)strtol(argv[4],NULL,10);
    run_role=argv[5]; run_label=argv[6];
    uint32_t max_regs=(uint32_t)strtoul(argv[7],NULL,10);
    uint64_t deadline_s=(uint64_t)strtoul(argv[8],NULL,10);
    const char *ledger_path=argv[9];
    int selftest=(argc>10 && !strcmp(argv[10],"selftest"));
    is_requester=0;
    (void)argc;
    signal(SIGTERM,sig_handler); signal(SIGINT,sig_handler);
    signal(SIGALRM,sig_handler);
    alarm((unsigned)(deadline_s+120));           /* last-resort watchdog */
    setup_device(local_ip,1);
    if(lg_open(&LG,ledger_path)) die_control("open ledger");
    lg_meta(&LG,0,run_label,run_role,"",-1,now_ns());
    connect_tcp(port,local_ip,peer_ip);
    send_msg(M_HELLO,(uintptr_t)buffer,PAGE_BYTES,MR_ACCESS_FLAGS,0);
    lg_control(&LG,++ctl_seq,C_RUN_READY,0,0,1,0,now_ns());
    recv_msg(M_HELLO);
    exchange_qp(&q_ctl,SLOT_CTL);
    abort_hook=owner_abort;
    index_first_tab=calloc((1u<<20)+2,4);
    uint64_t hsize=1; while(hsize<(uint64_t)max_regs*2+16) hsize<<=1;
    rkey_hash_keys=calloc(hsize,4); rkey_hash_gens=calloc(hsize,4);
    rkey_hash_mask=hsize-1;
    if(!index_first_tab||!rkey_hash_keys||!rkey_hash_gens) die_control("alloc tables");
    deadline_abs_ns=(uint64_t)deadline_s*1000000000ull;

    uint32_t gen=owner_register_gen();
    owner_note_recurrence();
    positive_control(gen,"pc-1");
    owner_send_batch();   /* gen 1 descriptor delivered+ACKed before any saved-descriptor use */
    if(selftest){
        /* machinery validation with the live gen-1 MR; never counted as
         * a recurrence sample (distinct ledger phases) */
        exchange_qp(&q_a,SLOT_A);
        probe_obs live=owner_request_probe(SLOT_A,PROBE_KIND_CURRENT,cur_rkey,
                                           gen,++probe_counter);
        owner_log_probe("st-live",gen,P_SELFTEST_LIVE,1,&q_a,0,cur_rkey,&live,1);
        destroy_qp_pair(&q_a,SLOT_A);
        if(!obs_accepted(&live)){ logline("selftest live probe not accepted");
            owner_abort(EXIT_CONTROL,"control-failure"); }
        uint32_t qs=quiesce_barrier();
        owner_dereg_gen(qs);
        exchange_qp(&q_b,SLOT_B);
        probe_obs post=owner_request_probe(SLOT_B,PROBE_KIND_SAVED,gen,
                                           gen,++probe_counter);
        owner_log_probe("st-post",gen,P_SELFTEST_POST,1,&q_b,1,cur_rkey,&post,0);
        destroy_qp_pair(&q_b,SLOT_B);
        if(!obs_rejected(&post)){ logline("selftest post-dereg probe not rejected");
            owner_abort(EXIT_MEMORY,"memory-mismatch"); }
    } else {
        uint32_t qs=quiesce_local();
        owner_dereg_gen(qs);
    }

    const char *stop_reason="count-budget";
    int stop_code=EXIT_OK;
    while(cur_gen<max_regs){
        if(stop_flag){ stop_reason="explicit-stop"; stop_code=EXIT_EXPLICIT; break; }
        if(first_reg_ns && now_ns()-first_reg_ns>=deadline_abs_ns){
            stop_reason="time-budget"; break; }
        gen=owner_register_gen();
        uint32_t saved_rkey_gen=0, saved_index_gen=0;
        int key_seen=rkey_hash_lookup(cur_rkey,&saved_rkey_gen);
        int idx_seen=(index_first_tab[cur_rkey>>8]!=0 &&
                      index_first_tab[cur_rkey>>8]!=gen);
        if(idx_seen) saved_index_gen=index_first_tab[cur_rkey>>8];
        owner_note_recurrence();
        if(key_seen && fk_samples<8){
            /* full-key recurrence: saved descriptor is bit-identical */
            fk_samples++;
            send_msg(M_SAMPLE_BEGIN,gen,3,0,0);
            recv_msg(M_SAMPLE_BEGIN);
            lg_control(&LG,++ctl_seq,C_SAMPLE_BEGIN,gen,3,0,0,now_ns());
            positive_control(gen,"pc-fk");
            exchange_qp(&q_a,SLOT_A);
            probe_obs live=owner_request_probe(SLOT_A,PROBE_KIND_SAVED,
                                               saved_rkey_gen,gen,++probe_counter);
            owner_log_probe("fkl",gen,P_FULLKEY_LIVE,1,&q_a,1,cur_rkey,&live,1);
            destroy_qp_pair(&q_a,SLOT_A);
            if(live.wr.posting_rc!=0) owner_abort(EXIT_CONTROL,"control-failure");
            if(live.wr.timed_out) owner_abort(EXIT_PROBE_TIMEOUT,"probe-timeout");
            if(live.wr.wc_status==0 && !obs_accepted(&live))
                owner_abort(EXIT_MEMORY,"memory-mismatch");
            uint32_t qs=quiesce_barrier();
            owner_dereg_gen(qs);
            exchange_qp(&q_b,SLOT_B);
            probe_obs post=owner_request_probe(SLOT_B,PROBE_KIND_SAVED,
                                               saved_rkey_gen,gen,++probe_counter);
            owner_log_probe("fkp",gen,P_FULLKEY_POST,1,&q_b,1,cur_rkey,&post,0);
            destroy_qp_pair(&q_b,SLOT_B);
            if(post.wr.posting_rc!=0) owner_abort(EXIT_CONTROL,"control-failure");
            if(post.wr.timed_out) owner_abort(EXIT_PROBE_TIMEOUT,"probe-timeout");
            if(post.wr.wc_status==0 && post.guards && strcmp(post.after,post.before)!=0)
                owner_abort(EXIT_MEMORY,"memory-mismatch");
        } else if(!key_seen && idx_seen && io_samples<4){
            io_samples++;
            send_msg(M_SAMPLE_BEGIN,gen,2,0,0);
            recv_msg(M_SAMPLE_BEGIN);
            lg_control(&LG,++ctl_seq,C_SAMPLE_BEGIN,gen,2,0,0,now_ns());
            positive_control(gen,"pc-io");
            exchange_qp(&q_a,SLOT_A);
            probe_obs pr=owner_request_probe(SLOT_A,PROBE_KIND_SAVED,
                                             saved_index_gen,gen,++probe_counter);
            owner_log_probe("io",gen,P_INDEX_ONLY,1,&q_a,1,cur_rkey,&pr,0);
            destroy_qp_pair(&q_a,SLOT_A);
            if(pr.wr.posting_rc!=0) owner_abort(EXIT_CONTROL,"control-failure");
            if(pr.wr.timed_out) owner_abort(EXIT_PROBE_TIMEOUT,"probe-timeout");
            if(pr.wr.wc_status==0 && pr.guards && strcmp(pr.after,pr.before)!=0)
                owner_abort(EXIT_MEMORY,"memory-mismatch");
            uint32_t qs=quiesce_barrier();
            owner_dereg_gen(qs);
        } else {
            uint32_t qs=quiesce_local();
            owner_dereg_gen(qs);
        }
        if(batch_fill>=BATCH) owner_send_batch();
    }
    owner_finish(stop_reason,stop_code);
    return 0; }

/* ================= REQUESTER ========================================= */
static struct qp_endpoint rq_ctl, rq_a, rq_b;
static uint32_t *hist_rkey;

static struct qp_endpoint *slot_qp(uint64_t slot){
    return slot==SLOT_A?&rq_a:(slot==SLOT_B?&rq_b:&rq_ctl); }

static int requester_main(int argc, char **argv){
    /* churn requester LOCAL PEER PORT ROLE LABEL MAXREGS DEADLINE_S LEDGER */
    const char *local_ip=argv[2], *peer_ip=argv[3];
    int port=(int)strtol(argv[4],NULL,10);
    run_role=argv[5]; run_label=argv[6];
    uint32_t max_regs=(uint32_t)strtoul(argv[7],NULL,10);
    (void)argv[8];
    const char *ledger_path=argv[9];
    is_requester=1;
    (void)argc;
    signal(SIGTERM,sig_handler); signal(SIGINT,sig_handler);
    setup_device(local_ip,0);
    if(lg_open(&LG,ledger_path)) die_control("open ledger");
    lg_meta(&LG,1,run_label,run_role,"",-1,now_ns());
    unsigned char *src=malloc(PAGE_BYTES);
    if(!src) die_control("alloc src");
    for(int i=0;i<PAGE_BYTES;i++) src[i]=(unsigned char)(0x3Cu^(unsigned)(i*11));
    struct ibv_mr *src_mr=ibv_reg_mr(pd,src,PAGE_BYTES,IBV_ACCESS_LOCAL_WRITE);
    if(!src_mr) die_control("reg src mr");
    src_lkey=src_mr->lkey;
    lg_registration(&LG,0,(uint32_t)src_mr->rkey,(uintptr_t)src,PAGE_BYTES,
                    (uintptr_t)pd,access_str,0,0,now_ns(),0);
    hist_rkey=calloc((size_t)max_regs+2,4);
    if(!hist_rkey) die_control("alloc history");
    connect_tcp(port,local_ip,peer_ip);
    struct message hello=recv_msg(M_HELLO);
    peer_base_addr=(uint64_t)hello.v1;
    lg_control(&LG,++ctl_seq,C_RUN_READY,0,0,1,1,now_ns());
    send_msg(M_HELLO,0,0,0,0);

    int running=1;
    while(running){
        struct message m=recv_msg(0);
        switch(m.kind){
        case M_QP_CONNECT:
            exchange_qp_reply(slot_qp(m.v1),m.v1);
            break;
        case M_QP_DESTROY:
            destroy_qp_pair(slot_qp(m.v1),m.v1);
            break;
        case M_DESC_BATCH:{
            uint32_t lo=(uint32_t)m.v1, count=(uint32_t)m.v2;
            if(count>BATCH||lo==0||lo+count-1>max_regs+1) die_control("batch range");
            uint32_t keys[BATCH];
            io_all(keys,(size_t)count*4,0);
            for(uint32_t i=0;i<count;i++){
                uint32_t g=lo+i, k=be32toh(keys[i]);
                if(g<=max_regs+1) hist_rkey[g]=k; }
            lg_control(&LG,++ctl_seq,C_DESC_RECEIVED,lo,lo+count-1,1,1,now_ns());
            lg_control(&LG,++ctl_seq,C_BATCH_ACK,lo,lo+count-1,1,1,now_ns());
            send_msg(M_BATCH_ACK,lo,count,0,0);
            break; }
        case M_PROBE_REQ:{
            int kind=(int)m.v1; uint32_t rk=(uint32_t)m.v2;
            uint64_t slot=m.v3;
            uint32_t payload_gen=(uint32_t)(m.v4>>32), tag=(uint32_t)(m.v4&0xFFFFFFFFu);
            uint32_t rkey;
            if(kind==PROBE_KIND_CURRENT) rkey=rk;
            else { if(rk==0||rk>max_regs+1||hist_rkey[rk]==0) die_control("saved descriptor not held");
                   rkey=hist_rkey[rk]; }
            fill_pattern(src,payload_gen,tag,1);  /* src is the registered source MR */
            struct qp_endpoint *e=slot_qp(slot);
            e->used=1;
            logline("probe post slot=%llu qpn=%u qp_state=%d rkey=%u remote=%llu",
                    (unsigned long long)slot, e->qp?e->qp->qp_num:0,
                    query_qp_state(e), rkey,
                    (unsigned long long)(peer_base_addr+DATA_OFFSET));
            write_result r=post_write(e,peer_base_addr+DATA_OFFSET,rkey,
                                      src,((uint64_t)payload_gen<<32)|tag);
            logline("probe done posting=%d wc=%d (%s) timed_out=%d qp_state=%d",
                    r.posting_rc, r.wc_status,
                    r.wc_status>=0?ibv_wc_status_str((enum ibv_wc_status)r.wc_status):"none",
                    r.timed_out, query_qp_state(e));
            lg_control(&LG,++ctl_seq,C_PROBE_RESULT,(uint32_t)r.posting_rc,
                       (uint32_t)r.wc_status,(uint32_t)(r.timed_out?1:0),1,now_ns());
            send_msg(M_PROBE_RESULT,(uint64_t)(int64_t)r.posting_rc,
                     (uint64_t)(int64_t)r.wc_status,(uint64_t)r.timed_out,0);
            break; }
        case M_QUIESCE:
            lg_control(&LG,++ctl_seq,C_QUIESCE,0,0,1,1,now_ns());
            send_msg(M_QUIESCE,0,0,0,0);
            break;
        case M_SAMPLE_BEGIN:
            lg_control(&LG,++ctl_seq,C_SAMPLE_BEGIN,(uint32_t)m.v1,(uint32_t)m.v2,0,1,now_ns());
            send_msg(M_SAMPLE_BEGIN,m.v1,m.v2,0,0);
            break;
        case M_RUN_END:
            lg_control(&LG,++ctl_seq,C_RUN_END,0,0,1,1,now_ns());
            send_msg(M_RUN_END,0,0,0,0);
            running=0;
            break;
        case M_ERROR:
            logline("peer reported error code=%llu",(unsigned long long)m.v1);
            running=0;
            break;
        default:
            die_control("unknown control kind"); } }

    destroy_qp_pair(&rq_ctl,SLOT_CTL);
    destroy_qp_pair(&rq_a,SLOT_A);
    destroy_qp_pair(&rq_b,SLOT_B);
    lg_meta(&LG,1,run_label,run_role,"",-1,now_ns());
    lg_close(&LG);
    if(sockfd>=0) close(sockfd);
    ibv_dereg_mr(src_mr);
    free(src);
    if(pd) ibv_dealloc_pd(pd);
    logline("requester finished");
    return 0; }

int main(int argc, char **argv){
    if(argc<10){
        fprintf(stderr,
          "usage: %s owner|requester LOCAL PEER PORT ROLE LABEL MAXREGS DEADLINE_S LEDGER [selftest]\n",
          argv[0]);
        return 2; }
    setvbuf(stdout,NULL,_IOLBF,0);
    signal(SIGPIPE,SIG_IGN);
    if(!strcmp(argv[1],"owner")) return owner_main(argc,argv);
    if(!strcmp(argv[1],"requester")) return requester_main(argc,argv);
    fprintf(stderr,"unknown role %s\n",argv[1]);
    return 2; }
