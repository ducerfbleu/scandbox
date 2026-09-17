// gateway.go---L7 audit proxy for the pi-agent standalone deployment (plane 1).
//
// Sits between the agent and the LLM on the internal network. The agent speaks
// plain HTTP to this gateway; the gateway reverse-proxies to the real LLM (http,
// https with or without cert verification, optionally via an egress proxy) and
// writes a structured, hash-chained JSONL record of every request/response —
// prompts, completions, tool calls, token usage, and timing (headers latency, first-token
// ttft_ms + decode_ms, and the backend `timings` object when present).
//
// Standard library ONLY (net/http, crypto/sha256, encoding/json, ...). Zero
// third-party dependencies. Build CGO_ENABLED=0 -> a static binary that runs on
// a `scratch` image.
//
// Config (env, set by run-pi-agent):
//   GATEWAY_LISTEN          bind address                 (default ":8080")
//   GATEWAY_UPSTREAM        real LLM base URL            (e.g. "https://llm:8080")  [required]
//   GATEWAY_TLS_VERIFY      "1" to verify upstream cert, else skip (default skip)
//   GATEWAY_CA              PEM CA bundle for verify     (optional; else system roots)
//   GATEWAY_UPSTREAM_PROXY  HTTP proxy for the upstream  (optional; remote LLM via squid)
//   GATEWAY_LOG             JSONL log path (append)      [required]
//   GATEWAY_RUN_ID          per-run provenance id        (default "unknown")
//   GATEWAY_MAX_CAPTURE     max bytes LOGGED per body    (default 8388608; forwarding is never truncated)
//
// Integrity: each record R carries prev_hash (previous record's record_hash; ""
// for the first) and record_hash = sha256( prev_hash || canonical(R) ), where
// canonical(R) is R marshaled with record_hash set to "". Any edit or deletion
// breaks the chain. Records are appended under a mutex, so chain order == append
// order. Verify a log with:  gateway -verify <logfile>  (same binary, identical
// canonicalization---no cross-language ambiguity).
//
// Safety: logging never blocks or corrupts forwarding. Request bodies are read in
// full for the upstream (only the LOGGED copy is bounded); response bodies are
// tee'd through a bounded buffer while every byte streams on to the agent. All
// parsing/logging happens off the hot path (on stream end) and is panic-guarded.

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ---- config ----------------------------------------------------------------

type config struct {
	listen        string
	upstream      *url.URL
	tlsVerify     bool
	caFile        string
	upstreamProxy *url.URL
	logPath       string
	runID         string
	maxCapture    int64
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func loadConfig() config {
	up := os.Getenv("GATEWAY_UPSTREAM")
	if up == "" {
		log.Fatal("GATEWAY_UPSTREAM is required")
	}
	upURL, err := url.Parse(up)
	if err != nil || upURL.Host == "" {
		log.Fatalf("bad GATEWAY_UPSTREAM %q: %v", up, err)
	}
	logPath := os.Getenv("GATEWAY_LOG")
	if logPath == "" {
		log.Fatal("GATEWAY_LOG is required")
	}
	var proxyURL *url.URL
	if p := os.Getenv("GATEWAY_UPSTREAM_PROXY"); p != "" {
		if proxyURL, err = url.Parse(p); err != nil {
			log.Fatalf("bad GATEWAY_UPSTREAM_PROXY %q: %v", p, err)
		}
	}
	maxCap, _ := strconv.ParseInt(env("GATEWAY_MAX_CAPTURE", "8388608"), 10, 64)
	if maxCap <= 0 {
		maxCap = 8 << 20
	}
	return config{
		listen:        env("GATEWAY_LISTEN", ":8080"),
		upstream:      upURL,
		tlsVerify:     os.Getenv("GATEWAY_TLS_VERIFY") == "1",
		caFile:        os.Getenv("GATEWAY_CA"),
		upstreamProxy: proxyURL,
		logPath:       logPath,
		runID:         env("GATEWAY_RUN_ID", "unknown"),
		maxCapture:    maxCap,
	}
}

func (c config) tlsClientConfig() *tls.Config {
	t := &tls.Config{InsecureSkipVerify: !c.tlsVerify}
	if c.tlsVerify && c.caFile != "" {
		pem, err := os.ReadFile(c.caFile)
		if err != nil {
			log.Fatalf("read CA %q: %v", c.caFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			log.Fatalf("no certs parsed from CA %q", c.caFile)
		}
		t.RootCAs = pool
	}
	return t
}

// ---- record & hash-chained logger ------------------------------------------

// Record is one request/response. Field order here defines the canonical form
// used for hashing (record_hash is always last and excluded from its own hash).
type Record struct {
	RunID            string          `json:"run_id"`
	Seq              uint64          `json:"seq"`
	TSRequest        string          `json:"ts_request"`
	TSResponse       string          `json:"ts_response,omitempty"`
	LatencyMS        int64           `json:"latency_ms"`
	TSFirstToken     string          `json:"ts_first_token,omitempty"`
	TSEnd            string          `json:"ts_end,omitempty"`
	TTFTMs           int64           `json:"ttft_ms,omitempty"`
	DecodeMS         int64           `json:"decode_ms,omitempty"`
	TotalMS          int64           `json:"total_ms,omitempty"`
	Method           string          `json:"method"`
	Path             string          `json:"path"`
	Endpoint         string          `json:"endpoint"`
	Model            string          `json:"model,omitempty"`
	Stream           bool            `json:"stream"`
	Status           int             `json:"status"`
	RequestBytes     int             `json:"request_bytes"`
	ResponseBytes    int             `json:"response_bytes"`
	Request          json.RawMessage `json:"request,omitempty"`
	Completion       string          `json:"completion,omitempty"`
	Reasoning        string          `json:"reasoning,omitempty"`
	ToolCalls        json.RawMessage `json:"tool_calls,omitempty"`
	Usage            json.RawMessage `json:"usage,omitempty"`
	Timings          json.RawMessage `json:"timings,omitempty"`
	FinishReason     string          `json:"finish_reason,omitempty"`
	PromptSHA256     string          `json:"prompt_sha256,omitempty"`
	CompletionSHA256 string          `json:"completion_sha256,omitempty"`
	ToolCallsSHA256  string          `json:"tool_calls_sha256,omitempty"`
	Truncated        bool            `json:"truncated,omitempty"`
	ParseError       string          `json:"parse_error,omitempty"`
	Error            string          `json:"error,omitempty"`
	PrevHash         string          `json:"prev_hash"`
	RecordHash       string          `json:"record_hash"`
}

type chainLogger struct {
	mu       sync.Mutex
	f        *os.File
	prevHash string
	seq      uint64
}

func newChainLogger(path string) *chainLogger {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		log.Fatalf("open log %q: %v", path, err)
	}
	return &chainLogger{f: f}
}

// write finalizes the chain fields, computes the record hash, and appends one
// JSONL line. Serialized; panic-guarded so a malformed record never kills a
// request goroutine.
func (c *chainLogger) write(rec *Record) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("logger panic: %v", r)
		}
	}()
	c.mu.Lock()
	defer c.mu.Unlock()
	c.seq++
	rec.Seq = c.seq
	rec.PrevHash = c.prevHash
	rec.RecordHash = ""
	canon, err := json.Marshal(rec)
	if err != nil {
		log.Printf("marshal record: %v", err)
		return
	}
	h := sha256.Sum256(append([]byte(c.prevHash), canon...))
	rec.RecordHash = hex.EncodeToString(h[:])
	c.prevHash = rec.RecordHash
	line, err := json.Marshal(rec)
	if err != nil {
		log.Printf("marshal line: %v", err)
		return
	}
	if _, err := c.f.Write(append(line, '\n')); err != nil {
		log.Printf("write log: %v", err)
	}
}

// ---- body capture (streaming-safe tee) -------------------------------------

// captureRC wraps the upstream response body. Read forwards every byte to the
// caller (the proxy copying to the agent) while buffering up to `max` bytes for
// the log. finalize fires exactly once, on EOF or Close.
type captureRC struct {
	src      io.ReadCloser
	buf      bytes.Buffer
	max      int64
	total    int64
	tsFirst  time.Time
	once     sync.Once
	finalize func(captured []byte, total int64, truncated bool, tsFirst, tsEnd time.Time)
}

func (c *captureRC) Read(p []byte) (int, error) {
	n, err := c.src.Read(p)
	if n > 0 {
		if c.tsFirst.IsZero() {
			c.tsFirst = time.Now().UTC() // first response byte ≈ first token (post-prefill)
		}
		c.total += int64(n)
		if room := c.max - int64(c.buf.Len()); room > 0 {
			w := int64(n)
			if w > room {
				w = room
			}
			c.buf.Write(p[:w])
		}
	}
	if err == io.EOF {
		c.done()
	}
	return n, err
}

func (c *captureRC) Close() error {
	c.done()
	return c.src.Close()
}

func (c *captureRC) done() {
	c.once.Do(func() {
		c.finalize(c.buf.Bytes(), c.total, c.total > int64(c.buf.Len()), c.tsFirst, time.Now().UTC())
	})
}

// ---- per-request metadata (carried via context) ----------------------------

type reqMeta struct {
	tsRequest    time.Time
	tsResponse   time.Time
	tsFirstToken time.Time
	tsEnd        time.Time
	method       string
	path         string
	status       int
	reqBody      []byte
	reqBytes     int
}

type ctxKey struct{}

func fromCtx(ctx context.Context) *reqMeta {
	if m, ok := ctx.Value(ctxKey{}).(*reqMeta); ok && m != nil {
		return m
	}
	return &reqMeta{tsRequest: time.Now().UTC()}
}

// ---- parsing helpers (OpenAI-compatible shapes) ----------------------------

func sha(b []byte) string    { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func shaStr(s string) string { return sha([]byte(s)) }

func appendErr(existing, e string) string {
	if existing == "" {
		return e
	}
	return existing + "; " + e
}

func normEndpoint(path string) string {
	switch {
	case strings.HasSuffix(path, "/chat/completions"):
		return "chat.completions"
	case strings.HasSuffix(path, "/completions"):
		return "completions"
	case strings.HasSuffix(path, "/embeddings"):
		return "embeddings"
	case strings.HasSuffix(path, "/models"):
		return "models"
	case strings.Contains(path, "props"):
		return "props"
	default:
		return path
	}
}

func (rec *Record) fillRequest(body []byte, maxCap int64) {
	var rp struct {
		Model    string          `json:"model"`
		Stream   bool            `json:"stream"`
		Messages json.RawMessage `json:"messages"`
		Prompt   json.RawMessage `json:"prompt"`
	}
	if err := json.Unmarshal(body, &rp); err != nil {
		rec.ParseError = appendErr(rec.ParseError, "request: "+err.Error())
		rec.PromptSHA256 = sha(body)
		return
	}
	rec.Model = rp.Model
	rec.Stream = rp.Stream
	switch {
	case len(rp.Messages) > 0:
		rec.PromptSHA256 = sha(rp.Messages)
	case len(rp.Prompt) > 0:
		rec.PromptSHA256 = sha(rp.Prompt)
	default:
		rec.PromptSHA256 = sha(body)
	}
	if int64(len(body)) <= maxCap {
		rec.Request = json.RawMessage(body)
	} else {
		rec.Truncated = true
	}
}

func (rec *Record) fillNonStream(body []byte) {
	var rp struct {
		Model   string `json:"model"`
		Choices []struct {
			Message struct {
				Content          string          `json:"content"`
				ReasoningContent string          `json:"reasoning_content"`
				ToolCalls        json.RawMessage `json:"tool_calls"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
		Usage   json.RawMessage `json:"usage"`
		Timings json.RawMessage `json:"timings"`
	}
	if err := json.Unmarshal(body, &rp); err != nil {
		rec.ParseError = appendErr(rec.ParseError, "response: "+err.Error())
		return
	}
	if rp.Model != "" {
		rec.Model = rp.Model
	}
	if len(rp.Usage) > 0 && string(rp.Usage) != "null" {
		rec.Usage = rp.Usage
	}
	if len(rp.Timings) > 0 && string(rp.Timings) != "null" {
		rec.Timings = rp.Timings
	}
	if len(rp.Choices) > 0 {
		rec.Completion = rp.Choices[0].Message.Content
		rec.Reasoning = rp.Choices[0].Message.ReasoningContent
		if len(rp.Choices[0].Message.ToolCalls) > 0 {
			rec.ToolCalls = rp.Choices[0].Message.ToolCalls
		}
		rec.FinishReason = rp.Choices[0].FinishReason
	}
}

// fillStream reconstructs a completion from an SSE stream of OpenAI chunks:
// concatenates delta.content, reassembles tool_call argument fragments by index,
// and keeps the last non-null usage / finish_reason.
func (rec *Record) fillStream(body []byte) {
	type tcAccum struct {
		id, typ, name string
		args          strings.Builder
	}
	var content strings.Builder
	var reasoning strings.Builder
	accums := map[int]*tcAccum{}
	var order []int
	var usage json.RawMessage
	var timings json.RawMessage
	var model, finish string

	sc := bufio.NewScanner(bytes.NewReader(body))
	sc.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(line[len("data:"):])
		if data == "" || data == "[DONE]" {
			continue
		}
		var ch struct {
			Model   string `json:"model"`
			Choices []struct {
				Delta struct {
					Content          string `json:"content"`
					ReasoningContent string `json:"reasoning_content"`
					ToolCalls        []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Type     string `json:"type"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					} `json:"tool_calls"`
				} `json:"delta"`
				FinishReason string `json:"finish_reason"`
			} `json:"choices"`
			Usage   json.RawMessage `json:"usage"`
			Timings json.RawMessage `json:"timings"`
		}
		if err := json.Unmarshal([]byte(data), &ch); err != nil {
			continue // tolerate stray/keepalive lines
		}
		if ch.Model != "" {
			model = ch.Model
		}
		if len(ch.Usage) > 0 && string(ch.Usage) != "null" {
			usage = ch.Usage
		}
		if len(ch.Timings) > 0 && string(ch.Timings) != "null" {
			timings = ch.Timings
		}
		if len(ch.Choices) == 0 {
			continue
		}
		content.WriteString(ch.Choices[0].Delta.Content)
		reasoning.WriteString(ch.Choices[0].Delta.ReasoningContent)
		if ch.Choices[0].FinishReason != "" {
			finish = ch.Choices[0].FinishReason
		}
		for _, tc := range ch.Choices[0].Delta.ToolCalls {
			a := accums[tc.Index]
			if a == nil {
				a = &tcAccum{}
				accums[tc.Index] = a
				order = append(order, tc.Index)
			}
			if tc.ID != "" {
				a.id = tc.ID
			}
			if tc.Type != "" {
				a.typ = tc.Type
			}
			if tc.Function.Name != "" {
				a.name = tc.Function.Name
			}
			a.args.WriteString(tc.Function.Arguments)
		}
	}
	if err := sc.Err(); err != nil {
		rec.ParseError = appendErr(rec.ParseError, "stream: "+err.Error())
	}

	rec.Completion = content.String()
	rec.Reasoning = reasoning.String()
	if model != "" {
		rec.Model = model
	}
	rec.FinishReason = finish
	rec.Usage = usage
	rec.Timings = timings
	if len(order) > 0 {
		sort.Ints(order)
		type fn struct {
			Name      string `json:"name"`
			Arguments string `json:"arguments"`
		}
		type tc struct {
			ID       string `json:"id,omitempty"`
			Type     string `json:"type,omitempty"`
			Function fn     `json:"function"`
		}
		out := make([]tc, 0, len(order))
		for _, i := range order {
			a := accums[i]
			out = append(out, tc{ID: a.id, Type: a.typ, Function: fn{Name: a.name, Arguments: a.args.String()}})
		}
		if b, err := json.Marshal(out); err == nil {
			rec.ToolCalls = b
		}
	}
}

func buildRecord(m *reqMeta, cfg config, captured []byte, total int64, truncated bool) *Record {
	rec := &Record{
		RunID:         cfg.runID,
		TSRequest:     m.tsRequest.Format(time.RFC3339Nano),
		Method:        m.method,
		Path:          m.path,
		Endpoint:      normEndpoint(m.path),
		Status:        m.status,
		RequestBytes:  m.reqBytes,
		ResponseBytes: int(total),
	}
	if !m.tsResponse.IsZero() {
		rec.TSResponse = m.tsResponse.Format(time.RFC3339Nano)
		rec.LatencyMS = m.tsResponse.Sub(m.tsRequest).Milliseconds()
	}
	parse := rec.Endpoint == "chat.completions" || rec.Endpoint == "completions"
	if parse && len(m.reqBody) > 0 {
		rec.fillRequest(m.reqBody, cfg.maxCapture)
	}
	if parse && len(captured) > 0 && m.status >= 200 && m.status < 300 {
		if rec.Stream {
			rec.fillStream(captured)
		} else {
			rec.fillNonStream(captured)
		}
	}
	if rec.Completion != "" {
		rec.CompletionSHA256 = shaStr(rec.Completion)
	}
	if len(rec.ToolCalls) > 0 {
		rec.ToolCallsSHA256 = sha(rec.ToolCalls)
	}
	if truncated {
		rec.Truncated = true
	}
	// per-phase timing (streaming only; a non-stream body's first byte ≈ its last, so the
	// prefill/decode split there must come from the backend `timings` object, not timestamps).
	if rec.Stream && !m.tsFirstToken.IsZero() {
		rec.TSFirstToken = m.tsFirstToken.Format(time.RFC3339Nano)
		rec.TTFTMs = m.tsFirstToken.Sub(m.tsRequest).Milliseconds()
	}
	if !m.tsEnd.IsZero() {
		rec.TSEnd = m.tsEnd.Format(time.RFC3339Nano)
		rec.TotalMS = m.tsEnd.Sub(m.tsRequest).Milliseconds()
		if rec.Stream && !m.tsFirstToken.IsZero() {
			rec.DecodeMS = m.tsEnd.Sub(m.tsFirstToken).Milliseconds()
		}
	}
	return rec
}

// ---- verify mode -----------------------------------------------------------

func verifyLog(path string) int {
	f, err := os.Open(path)
	if err != nil {
		log.Printf("open %q: %v", path, err)
		return 1
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 64*1024*1024)
	prev := ""
	var last uint64
	n := 0
	for sc.Scan() {
		if len(bytes.TrimSpace(sc.Bytes())) == 0 {
			continue
		}
		var rec Record
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			log.Printf("line %d: bad JSON: %v", n+1, err)
			return 1
		}
		stored := rec.RecordHash
		if rec.PrevHash != prev {
			log.Printf("seq %d: prev_hash mismatch---chain broken (record inserted/deleted)", rec.Seq)
			return 1
		}
		if rec.Seq != last+1 {
			log.Printf("seq gap: got %d after %d", rec.Seq, last)
			return 1
		}
		rec.RecordHash = ""
		canon, _ := json.Marshal(&rec)
		h := sha256.Sum256(append([]byte(rec.PrevHash), canon...))
		if hex.EncodeToString(h[:]) != stored {
			log.Printf("seq %d: record_hash mismatch---record tampered", rec.Seq)
			return 1
		}
		last = rec.Seq
		prev = stored
		n++
	}
	if err := sc.Err(); err != nil {
		log.Printf("scan: %v", err)
		return 1
	}
	log.Printf("OK: %d records, chain intact, head=%s", n, prev)
	// anti-truncation: compare the tip against the anchored head in the sibling index.json (if any).
	// The chain alone can't catch a chopped tail---a truncated suffix is still self-consistent.
	if b, err := os.ReadFile(filepath.Join(filepath.Dir(path), "index.json")); err == nil {
		var idx struct {
			Chain struct {
				Head string `json:"head"`
				Len  int    `json:"len"`
			} `json:"chain"`
		}
		if json.Unmarshal(b, &idx) == nil && idx.Chain.Head != "" {
			if idx.Chain.Head != prev || idx.Chain.Len != n {
				log.Printf("ANCHOR MISMATCH---log tip (head=%s len=%d) != index.json anchor (head=%s len=%d): "+
					"tail truncated or tampered", prev, n, idx.Chain.Head, idx.Chain.Len)
				return 1
			}
			log.Printf("anchor OK: tip matches index.json (len=%d)", n)
		}
	}
	return 0
}

// ---- main ------------------------------------------------------------------

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	if len(os.Args) >= 3 && os.Args[1] == "-verify" {
		os.Exit(verifyLog(os.Args[2]))
	}

	cfg := loadConfig()
	cl := newChainLogger(cfg.logPath)

	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.TLSClientConfig = cfg.tlsClientConfig()
	if cfg.upstreamProxy != nil {
		tr.Proxy = http.ProxyURL(cfg.upstreamProxy) // remote LLM egresses via the allowlist proxy
	} else {
		tr.Proxy = nil // never fall through to env proxies
	}

	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(cfg.upstream)
			pr.Out.Host = cfg.upstream.Host
		},
		Transport:     tr,
		FlushInterval: -1, // flush each write---token streaming stays real-time
		ModifyResponse: func(resp *http.Response) error {
			m := fromCtx(resp.Request.Context())
			// tsResponse = response-HEADERS time (llama.cpp streaming flushes these ~immediately,
			// so latency_ms ≈ accept latency). Real phase timing: first response BYTE (captureRC
			// -> ttft_ms ≈ prefill) and stream END (finalize -> decode_ms), plus the backend
			// `timings` object captured in fill{Stream,NonStream} for exact prefill/decode tk/s.
			m.tsResponse = time.Now().UTC()
			m.status = resp.StatusCode
			resp.Body = &captureRC{
				src: resp.Body,
				max: cfg.maxCapture,
				finalize: func(captured []byte, total int64, truncated bool, tsFirst, tsEnd time.Time) {
					defer func() {
						if r := recover(); r != nil {
							log.Printf("finalize panic: %v", r)
						}
					}()
					m.tsFirstToken = tsFirst
					m.tsEnd = tsEnd
					cl.write(buildRecord(m, cfg, captured, total, truncated))
				},
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			m := fromCtx(r.Context())
			m.status = http.StatusBadGateway
			rec := buildRecord(m, cfg, nil, 0, false)
			rec.TSResponse = time.Now().UTC().Format(time.RFC3339Nano)
			rec.LatencyMS = time.Now().UTC().Sub(m.tsRequest).Milliseconds()
			rec.Error = err.Error()
			cl.write(rec)
			w.WriteHeader(http.StatusBadGateway)
			io.WriteString(w, "gateway: upstream error\n")
		},
	}

	handler := func(w http.ResponseWriter, r *http.Request) {
		m := &reqMeta{tsRequest: time.Now().UTC(), method: r.Method, path: r.URL.Path}
		if r.Body != nil {
			full, _ := io.ReadAll(r.Body) // full body forwarded; only the logged copy is bounded
			r.Body.Close()
			m.reqBytes = len(full)
			m.reqBody = full
			r.Body = io.NopCloser(bytes.NewReader(full))
			r.ContentLength = int64(len(full))
			r.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(full)), nil }
		}
		proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxKey{}, m)))
	}

	srv := &http.Server{
		Addr:              cfg.listen,
		Handler:           http.HandlerFunc(handler),
		ReadHeaderTimeout: 30 * time.Second,
		// no Write/Idle timeouts: LLM streams can run long
	}
	log.Printf("gateway: listen %s -> %s (tls_verify=%v proxy=%v) log=%s run=%s max_capture=%d",
		cfg.listen, cfg.upstream, cfg.tlsVerify, cfg.upstreamProxy, cfg.logPath, cfg.runID, cfg.maxCapture)
	log.Fatal(srv.ListenAndServe())
}
