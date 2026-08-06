package tools

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"unicode/utf8"
)

const maxOutputRecordBytes = 64 * 1024
const outputRedactionOverlapBytes = 4 * 1024

var sensitiveRecordIntroducers = []string{
	"token=", "secret=", "password=", "api_key=", "api-key=",
	"bearer ", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "npm_",
	"-----begin ",
}

// streamCapture redacts before persistence and keeps only bounded response
// windows in RAM. The durable spool remains the full source of truth.
type streamCapture struct {
	mu                   sync.Mutex
	file                 *os.File
	headLimit, tailLimit int
	head, tail           []byte
	pending              []byte
	total                int64
	privateKey           bool
	oversized            bool
	mirror               func([]byte) error
	mirrorRollback       func(int64)
}

func newStreamCapture(path string, headLimit, tailLimit int) (*streamCapture, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	return &streamCapture{file: file, headLimit: max(headLimit, 0), tailLimit: max(tailLimit, 0)}, nil
}

func newMemoryStreamCapture(limit int) *streamCapture {
	return &streamCapture{headLimit: max(limit, 0)}
}

func (c *streamCapture) Write(data []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.pending = append(c.pending, data...)
	for {
		newline := bytes.IndexByte(c.pending, '\n')
		if newline < 0 {
			if len(c.pending) <= maxOutputRecordBytes {
				break
			}
			if c.oversized || recordMayContainSecret(c.pending) {
				if !c.oversized {
					if err := c.emit([]byte("[REDACTED OVERSIZED SECRET RECORD]")); err != nil {
						return 0, err
					}
					c.oversized = true
				}
				// Once a record is security-ambiguous, fail closed until its
				// delimiter. Memory remains bounded without persisting a suffix.
				c.pending = c.pending[len(c.pending)-outputRedactionOverlapBytes:]
				break
			}
			// Long ordinary records are not truncation. Persist their safe
			// prefix exactly while retaining enough overlap to recognize a
			// secret introducer split across future writes.
			flush := len(c.pending) - outputRedactionOverlapBytes
			if err := c.emit([]byte(redact(string(c.pending[:flush])))); err != nil {
				return 0, err
			}
			c.pending = append(c.pending[:0], c.pending[flush:]...)
			break
		}
		line := append([]byte(nil), c.pending[:newline+1]...)
		c.pending = c.pending[newline+1:]
		if c.oversized {
			c.oversized = false
			if err := c.emit([]byte("\n")); err != nil {
				return 0, err
			}
			continue
		}
		if err := c.emitRedactedLine(line); err != nil {
			return 0, err
		}
	}
	return len(data), nil
}

func recordMayContainSecret(record []byte) bool {
	lower := strings.ToLower(string(record))
	for _, introducer := range sensitiveRecordIntroducers {
		if strings.Contains(lower, introducer) {
			return true
		}
	}
	// Credential-bearing URLs may use arbitrary schemes accepted by command
	// output. The user-info shape is the sensitive part we must fail closed on.
	return strings.Contains(lower, "://") && strings.Contains(lower, "@")
}

func (c *streamCapture) emitRedactedLine(line []byte) error {
	text := string(line)
	upper := strings.ToUpper(text)
	if c.privateKey {
		if strings.Contains(upper, "-----END ") && strings.Contains(upper, "PRIVATE KEY-----") {
			c.privateKey = false
		}
		return nil
	}
	if strings.Contains(upper, "-----BEGIN ") && strings.Contains(upper, "PRIVATE KEY-----") {
		c.privateKey = true
		return c.emit([]byte("[REDACTED PRIVATE KEY]\n"))
	}
	return c.emit([]byte(redact(text)))
}

func (c *streamCapture) emit(data []byte) error {
	if c.mirror != nil {
		if err := c.mirror(data); err != nil {
			return err
		}
	}
	if c.file != nil {
		if n, err := c.file.Write(data); err != nil {
			if c.mirrorRollback != nil && n < len(data) {
				c.mirrorRollback(int64(len(data) - n))
			}
			return err
		} else if n != len(data) {
			if c.mirrorRollback != nil {
				c.mirrorRollback(int64(len(data) - n))
			}
			return io.ErrShortWrite
		}
	}
	c.total += int64(len(data))
	if len(c.head) < c.headLimit {
		need := min(c.headLimit-len(c.head), len(data))
		c.head = append(c.head, data[:need]...)
	}
	if c.tailLimit > 0 {
		c.tail = append(c.tail, data...)
		if len(c.tail) > c.tailLimit {
			c.tail = append([]byte(nil), c.tail[len(c.tail)-c.tailLimit:]...)
		}
	}
	return nil
}

func (c *streamCapture) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.pending) > 0 && !c.oversized {
		if err := c.emitRedactedLine(c.pending); err != nil {
			if c.file != nil {
				_ = c.file.Close()
			}
			return err
		}
	}
	c.pending = nil
	if c.file == nil {
		return nil
	}
	if err := c.file.Sync(); err != nil {
		_ = c.file.Close()
		return err
	}
	return c.file.Close()
}

func (c *streamCapture) Head() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return utf8Window(c.head, false)
}

func (c *streamCapture) Preview(limit int) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if limit <= 0 {
		return ""
	}
	head := utf8Window(c.head, false)
	if c.total <= int64(len(c.head)) {
		return head
	}
	return headTailText(head+inlineOmissionMarker+utf8Window(c.tail, true), limit)
}
func (c *streamCapture) TailLines(lines int) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return tailText(utf8Window(c.tail, true), lines)
}

func utf8Window(data []byte, trimPrefix bool) string {
	start, end := 0, len(data)
	if trimPrefix {
		for start < end && !utf8.RuneStart(data[start]) {
			start++
		}
	}
	for end > start && !utf8.Valid(data[start:end]) {
		end--
	}
	text := strings.ToValidUTF8(string(data[start:end]), "�")
	for len(text) > len(data) {
		_, size := utf8.DecodeLastRuneInString(text)
		text = text[:len(text)-size]
	}
	return text
}
func (c *streamCapture) Total() int64 { c.mu.Lock(); defer c.mu.Unlock(); return c.total }

type commandCapture struct {
	stdout, stderr *streamCapture
	artifact       *artifactWriter
}

func newCommandCapture(root, id string, limit int, store *artifactStore) (*commandCapture, error) {
	out, err := newStreamCapture(filepath.Join(root, id+".stdout"), limit, limit)
	if err != nil {
		return nil, err
	}
	errStream, err := newStreamCapture(filepath.Join(root, id+".stderr"), limit, limit)
	if err != nil {
		_ = out.Close()
		return nil, err
	}
	artifact, err := store.create(id)
	if err != nil {
		_ = out.Close()
		_ = errStream.Close()
		_ = os.Remove(filepath.Join(root, id+".stdout"))
		_ = os.Remove(filepath.Join(root, id+".stderr"))
		return nil, err
	}
	artifact.companionCopies = 1
	out.mirror = func(data []byte) error { return artifact.WriteRecord(1, data) }
	errStream.mirror = func(data []byte) error { return artifact.WriteRecord(2, data) }
	out.mirrorRollback = func(amount int64) { store.rollbackWrite(id, amount) }
	errStream.mirrorRollback = func(amount int64) { store.rollbackWrite(id, amount) }
	return &commandCapture{stdout: out, stderr: errStream, artifact: artifact}, nil
}

func (c *commandCapture) Close() error {
	left, right := c.stdout.Close(), c.stderr.Close()
	if left != nil {
		_ = c.artifact.Abort()
		return left
	}
	if right != nil {
		_ = c.artifact.Abort()
		return right
	}
	return c.artifact.Close()
}

var _ io.Writer = (*streamCapture)(nil)

func boundedLogPage(path string, startLine, endLine, maxBytes int, expression *regexp.Regexp) (string, int, int, int, bool, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, 0, 0, false, err
	}
	defer file.Close()
	reader := bufio.NewScanner(file)
	reader.Buffer(make([]byte, 64*1024), maxOutputRecordBytes)
	var builder strings.Builder
	line, first, last, total := 0, 0, 0, 0
	truncated := false
	for reader.Scan() {
		line++
		total = line
		if line < startLine || (endLine > 0 && line > endLine) || (expression != nil && !expression.MatchString(reader.Text())) {
			continue
		}
		row := reader.Text()
		need := len(row)
		if builder.Len() > 0 {
			need++
		}
		if builder.Len()+need > maxBytes {
			truncated = true
			continue
		}
		if builder.Len() > 0 {
			builder.WriteByte('\n')
		}
		builder.WriteString(row)
		if first == 0 {
			first = line
		}
		last = line
	}
	if err := reader.Err(); err != nil {
		return "", 0, 0, total, false, err
	}
	return builder.String(), first, last, total, truncated, nil
}

func outputPersistenceError(err error) map[string]any {
	return failure("output_persistence_failed", fmt.Sprintf("persist command output: %v", err))
}
