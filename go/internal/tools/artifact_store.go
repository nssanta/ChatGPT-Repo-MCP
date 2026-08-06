package tools

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	defaultArtifactTotalBytes = int64(10 * 1024 * 1024 * 1024)
	defaultArtifactMaxBytes   = int64(5 * 1024 * 1024 * 1024)
	defaultArtifactTTL        = 7 * 24 * time.Hour
	defaultArtifactReserve    = int64(2 * 1024 * 1024 * 1024)
)

type artifactStore struct {
	mu            sync.Mutex
	root          string
	totalLimit    int64
	itemLimit     int64
	ttl           time.Duration
	reserve       int64
	usage         int64
	active        map[string]int
	artifactUse   map[string]int64
	diskChecked   time.Time
	diskAvailable int64
	diskCapacity  int64
	cursorKey     []byte
	ioLocks       map[string]*sync.RWMutex
	logicalSizes  map[string]int64
	committedEnds map[string]int64
	createdTimes  map[string]time.Time
	artifactKinds map[string]string
	orderings     map[string]string
}

type artifactWriter struct {
	store           *artifactStore
	id              string
	file            *os.File
	once            sync.Once
	mu              sync.Mutex
	ioLock          *sync.RWMutex
	companionCopies int64
	kind            string
	ordering        string
}

func newArtifactStore(root string, totalLimit, itemLimit int64, ttl time.Duration, reserve int64) (*artifactStore, error) {
	if totalLimit <= 0 {
		totalLimit = defaultArtifactTotalBytes
	}
	if itemLimit <= 0 {
		itemLimit = defaultArtifactMaxBytes
	}
	if ttl <= 0 {
		ttl = defaultArtifactTTL
	}
	if reserve < 0 {
		reserve = defaultArtifactReserve
	}
	root = filepath.Join(root, "artifacts")
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	s := &artifactStore{root: root, totalLimit: totalLimit, itemLimit: itemLimit, ttl: ttl, reserve: reserve, active: map[string]int{}, artifactUse: map[string]int64{}, ioLocks: map[string]*sync.RWMutex{}, logicalSizes: map[string]int64{}, committedEnds: map[string]int64{}, createdTimes: map[string]time.Time{}, artifactKinds: map[string]string{}, orderings: map[string]string{}}
	key, err := loadArtifactCursorKey(root)
	if err != nil {
		return nil, err
	}
	s.cursorKey = key
	if err := s.scan(); err != nil {
		return nil, err
	}
	if err := s.cleanupLocked(0); err != nil {
		return nil, err
	}
	return s, nil
}

func loadArtifactCursorKey(root string) ([]byte, error) {
	path := filepath.Join(root, ".cursor-key")
	if key, err := readFileLimited(path, 32); err == nil {
		if len(key) != 32 {
			return nil, errors.New("artifact cursor key has invalid length")
		}
		return key, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, os.ErrExist) {
		return loadArtifactCursorKey(root)
	}
	if err != nil {
		return nil, err
	}
	if n, err := file.Write(key); err != nil || n != len(key) {
		_ = file.Close()
		_ = os.Remove(path)
		if err != nil {
			return nil, err
		}
		return nil, io.ErrShortWrite
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return nil, err
	}
	if err := file.Close(); err != nil {
		return nil, err
	}
	return key, nil
}

func (s *artifactStore) scan() error {
	s.usage = 0
	ids := map[string]bool{}
	collect := func(directory string, suffixes ...string) error {
		entries, err := os.ReadDir(directory)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if entry.IsDir() || strings.HasPrefix(entry.Name(), ".") {
				continue
			}
			for _, suffix := range suffixes {
				if strings.HasSuffix(entry.Name(), suffix) {
					id := stringsTrimSuffix(entry.Name(), suffix)
					if artifactIDPattern.MatchString(id) && id != "." && id != ".." {
						ids[id] = true
					}
					break
				}
			}
		}
		return nil
	}
	base := filepath.Dir(s.root)
	if err := collect(s.root, ".records", ".complete"); err != nil {
		return err
	}
	if err := collect(base, ".stdout", ".stderr", ".json"); err != nil {
		return err
	}
	if err := collect(filepath.Join(base, "logs"), ".combined", ".json"); err != nil {
		return err
	}
	for id := range ids {
		recordsPath := filepath.Join(s.root, id+".records")
		combined := int64(0)
		createdAt := time.Time{}
		if info, err := os.Stat(recordsPath); err == nil {
			combined += info.Size()
			createdAt = info.ModTime()
		}
		for _, companion := range s.companionPaths(id) {
			if companionInfo, err := os.Stat(companion); err == nil {
				combined += companionInfo.Size()
				if createdAt.IsZero() || companionInfo.ModTime().Before(createdAt) {
					createdAt = companionInfo.ModTime()
				}
			}
		}
		s.usage += combined
		s.artifactUse[id+".records"] = combined
		s.createdTimes[id] = createdAt
		_, combinedErr := os.Stat(filepath.Join(base, "logs", id+".combined"))
		_, terminalMetadataErr := os.Stat(filepath.Join(base, "logs", id+".json"))
		if combinedErr == nil || terminalMetadataErr == nil {
			s.artifactKinds[id] = "pty"
			s.orderings[id] = "capture_order"
		}
		if _, err := os.Stat(recordsPath); err == nil {
			logical, end, scanErr := scanArtifactFrames(recordsPath, s.itemLimit)
			if scanErr != nil {
				return scanErr
			}
			s.logicalSizes[id], s.committedEnds[id] = logical, end
		}
	}
	return nil
}

func (s *artifactStore) create(id string) (*artifactWriter, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.active[id] != 0 {
		return nil, fmt.Errorf("artifact %q is already active", id)
	}
	if err := s.cleanupLocked(0); err != nil {
		return nil, err
	}
	path := filepath.Join(s.root, id+".records")
	completionPath := filepath.Join(s.root, id+".complete")
	_ = os.Remove(completionPath)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	companionTotal := int64(0)
	for _, companion := range s.companionPaths(id) {
		if info, statErr := os.Stat(companion); statErr == nil {
			companionTotal += info.Size()
		}
	}
	previousTotal := s.artifactUse[id+".records"]
	delta := companionTotal - previousTotal
	s.usage += delta
	if !s.diskChecked.IsZero() {
		s.diskAvailable -= delta
	}
	s.artifactUse[id+".records"] = companionTotal
	s.logicalSizes[id], s.committedEnds[id] = 0, 0
	s.createdTimes[id] = time.Now().UTC()
	s.artifactKinds[id] = "command_output"
	s.orderings[id] = "stdout_then_stderr"
	s.active[id]++
	ioLock := s.ioLocks[id]
	if ioLock == nil {
		ioLock = &sync.RWMutex{}
		s.ioLocks[id] = ioLock
	}
	return &artifactWriter{store: s, id: id, file: file, ioLock: ioLock}, nil
}

func (w *artifactWriter) setMetadata(kind, ordering string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.kind, w.ordering = kind, ordering
	w.store.mu.Lock()
	w.store.artifactKinds[w.id] = kind
	w.store.orderings[w.id] = ordering
	w.store.mu.Unlock()
}

func (s *artifactStore) reserveWrite(id string, physicalAmount, logicalAmount int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	name := id + ".records"
	if s.logicalSizes[id]+logicalAmount > s.itemLimit {
		return fmt.Errorf("artifact_quota: artifact exceeds %d bytes", s.itemLimit)
	}
	if s.usage+physicalAmount > s.totalLimit {
		if err := s.cleanupLocked(physicalAmount); err != nil {
			return err
		}
	}
	if time.Since(s.diskChecked) >= time.Second || s.diskChecked.IsZero() {
		available, capacity, err := artifactDiskSpace(s.root)
		if err != nil {
			return fmt.Errorf("artifact disk check: %w", err)
		}
		s.diskAvailable, s.diskCapacity, s.diskChecked = available, capacity, time.Now()
	}
	reserve := max(s.reserve, s.diskCapacity/10)
	if s.diskAvailable-physicalAmount < reserve {
		return fmt.Errorf("artifact_quota: disk reserve %d bytes would be crossed", reserve)
	}
	s.diskAvailable -= physicalAmount
	s.usage += physicalAmount
	s.artifactUse[name] += physicalAmount
	return nil
}

func (s *artifactStore) rollbackWrite(id string, amount int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	name := id + ".records"
	s.usage -= amount
	s.artifactUse[name] -= amount
	s.diskAvailable += amount
}

func (s *artifactStore) writeCompanion(id, path string, content []byte) error {
	s.mu.Lock()
	s.active[id]++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.active[id]--
		if s.active[id] == 0 {
			delete(s.active, id)
		}
		s.mu.Unlock()
	}()
	previous := int64(0)
	if info, err := os.Stat(path); err == nil {
		previous = info.Size()
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	reserved := int64(len(content))
	if err := s.reserveWrite(id, reserved, 0); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		s.rollbackWrite(id, reserved)
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".artifact-companion-*")
	if err != nil {
		s.rollbackWrite(id, reserved)
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err = temporary.Chmod(0o600); err == nil {
		_, err = temporary.Write(content)
	}
	if err == nil {
		err = temporary.Sync()
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = replaceArtifactFile(temporaryPath, path)
	}
	if err != nil {
		s.rollbackWrite(id, reserved)
		return err
	}
	if previous > 0 {
		s.rollbackWrite(id, previous)
	}
	return nil
}

func (s *artifactStore) cleanupLocked(incoming int64) error {
	type candidate struct {
		name    string
		mod     time.Time
		size    int64
		expired bool
	}
	now := time.Now()
	candidates := make([]candidate, 0, len(s.artifactUse))
	for name, trackedSize := range s.artifactUse {
		id := stringsTrimSuffix(name, ".records")
		if s.active[id] > 0 {
			continue
		}
		mod := time.Time{}
		for _, path := range append([]string{filepath.Join(s.root, name)}, s.companionPaths(id)...) {
			if info, err := os.Stat(path); err == nil && info.ModTime().After(mod) {
				mod = info.ModTime()
			}
		}
		if mod.IsZero() {
			delete(s.artifactUse, name)
			delete(s.logicalSizes, id)
			delete(s.committedEnds, id)
			delete(s.createdTimes, id)
			delete(s.artifactKinds, id)
			delete(s.orderings, id)
			continue
		}
		candidates = append(candidates, candidate{name, mod, trackedSize, now.Sub(mod) > s.ttl})
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].expired != candidates[j].expired {
			return candidates[i].expired
		}
		return candidates[i].mod.Before(candidates[j].mod)
	})
	for _, candidate := range candidates {
		if !candidate.expired && s.usage+incoming <= s.totalLimit {
			break
		}
		if err := os.Remove(filepath.Join(s.root, candidate.name)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		id := stringsTrimSuffix(candidate.name, ".records")
		for _, companion := range s.companionPaths(id) {
			if err := os.Remove(companion); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		s.usage -= candidate.size
		s.diskAvailable += candidate.size
		delete(s.artifactUse, candidate.name)
		delete(s.logicalSizes, id)
		delete(s.committedEnds, id)
		delete(s.createdTimes, id)
		delete(s.artifactKinds, id)
		delete(s.orderings, id)
		delete(s.ioLocks, id)
	}
	if s.usage+incoming > s.totalLimit {
		return fmt.Errorf("artifact_quota: store exceeds %d bytes", s.totalLimit)
	}
	return nil
}

func (s *artifactStore) companionPaths(id string) []string {
	base := filepath.Dir(s.root)
	return []string{filepath.Join(base, id+".stdout"), filepath.Join(base, id+".stderr"), filepath.Join(base, id+".json"), filepath.Join(base, "logs", id+".combined"), filepath.Join(base, "logs", id+".json"), filepath.Join(s.root, id+".complete")}
}

func stringsTrimSuffix(value, suffix string) string {
	if len(value) >= len(suffix) && value[len(value)-len(suffix):] == suffix {
		return value[:len(value)-len(suffix)]
	}
	return value
}

func scanArtifactFrames(path string, itemLimit int64) (int64, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, 0, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return 0, 0, err
	}
	var logical, physical int64
	for {
		var header [9]byte
		_, err := io.ReadFull(file, header[:])
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			return logical, physical, nil
		}
		if err != nil {
			return 0, 0, fmt.Errorf("artifact_io: scan header: %w", err)
		}
		length := int64(binary.BigEndian.Uint64(header[1:]))
		if length < 0 || length > itemLimit {
			return 0, 0, errors.New("artifact_io: invalid frame length")
		}
		if physical+9+length > info.Size() {
			return logical, physical, nil
		}
		if _, err := file.Seek(length, io.SeekCurrent); err != nil {
			return 0, 0, err
		}
		physical += 9 + length
		logical += length
	}
}

func (w *artifactWriter) WriteRecord(stream byte, data []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.ioLock.Lock()
	defer w.ioLock.Unlock()
	frameBytes := int64(1+8+len(data)) + w.companionCopies*int64(len(data))
	if err := w.store.reserveWrite(w.id, frameBytes, int64(len(data))); err != nil {
		return err
	}
	start, err := w.file.Seek(0, io.SeekCurrent)
	if err != nil {
		w.store.rollbackWrite(w.id, frameBytes)
		return fmt.Errorf("artifact_io: seek: %w", err)
	}
	header := make([]byte, 9)
	header[0] = stream
	binary.BigEndian.PutUint64(header[1:], uint64(len(data)))
	if n, err := w.file.Write(header); err != nil || n != len(header) {
		w.store.rollbackWrite(w.id, frameBytes)
		_ = w.file.Truncate(start)
		if err != nil {
			return fmt.Errorf("artifact_io: write header: %w", err)
		}
		return fmt.Errorf("artifact_io: write header: %w", io.ErrShortWrite)
	}
	if n, err := w.file.Write(data); err != nil || n != len(data) {
		w.store.rollbackWrite(w.id, frameBytes)
		_ = w.file.Truncate(start)
		_, _ = w.file.Seek(start, io.SeekStart)
		if err != nil {
			return fmt.Errorf("artifact_io: write payload: %w", err)
		}
		return fmt.Errorf("artifact_io: write payload: %w", io.ErrShortWrite)
	}
	w.store.mu.Lock()
	w.store.logicalSizes[w.id] += int64(len(data))
	w.store.committedEnds[w.id] = start + int64(len(header)+len(data))
	w.store.mu.Unlock()
	return nil
}

func (w *artifactWriter) Close() error {
	return w.finish(true)
}

func (w *artifactWriter) Abort() error { return w.finish(false) }

func (w *artifactWriter) finish(complete bool) error {
	var result error
	w.once.Do(func() {
		w.ioLock.Lock()
		defer w.ioLock.Unlock()
		if err := w.file.Sync(); err != nil {
			result = err
		} else {
			result = w.file.Close()
		}
		if result == nil && complete {
			result = w.store.writeCompletionMarker(w.id, w.kind, w.ordering)
		}
		w.store.mu.Lock()
		w.store.active[w.id]--
		if w.store.active[w.id] == 0 {
			delete(w.store.active, w.id)
		}
		w.store.mu.Unlock()
	})
	return result
}

type artifactCompletion struct {
	Size      int64     `json:"size"`
	SHA256    string    `json:"sha256"`
	CreatedAt time.Time `json:"created_at"`
	Kind      string    `json:"kind,omitempty"`
	Ordering  string    `json:"ordering,omitempty"`
}

func (s *artifactStore) writeCompletionMarker(id, kind, ordering string) error {
	records, err := os.Open(filepath.Join(s.root, id+".records"))
	if err != nil {
		return err
	}
	size, digest, err := logicalArtifactMetadata(records, s.itemLimit)
	_ = records.Close()
	if err != nil {
		return err
	}
	s.mu.Lock()
	createdAt := s.createdTimes[id]
	s.mu.Unlock()
	content, err := json.Marshal(artifactCompletion{Size: size, SHA256: digest, CreatedAt: createdAt, Kind: kind, Ordering: ordering})
	if err != nil {
		return err
	}
	content = append(content, '\n')
	if err := s.reserveWrite(id, int64(len(content)), 0); err != nil {
		return err
	}
	if err := writeArtifactCompletionMarker(filepath.Join(s.root, id+".complete"), content); err != nil {
		s.rollbackWrite(id, int64(len(content)))
		return err
	}
	return nil
}

func writeArtifactCompletionMarker(path string, content []byte) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if n, err := file.Write(content); err != nil || n != len(content) {
		_ = file.Close()
		_ = os.Remove(path)
		if err != nil {
			return err
		}
		return io.ErrShortWrite
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		_ = os.Remove(path)
		return err
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(path)
		return err
	}
	return nil
}

type artifactCursor struct {
	phase   byte
	offset  int64
	intra   int64
	logical int64
}

func (s *artifactStore) encodeArtifactCursor(id, payload string, cursor artifactCursor) string {
	body := fmt.Sprintf("%d:%d:%d:%d", cursor.phase, cursor.offset, cursor.intra, cursor.logical)
	mac := hmac.New(sha256.New, s.cursorKey)
	_, _ = mac.Write([]byte(id + "\x00" + payload + "\x00" + body))
	return base64.RawURLEncoding.EncodeToString([]byte(body + ":" + hex.EncodeToString(mac.Sum(nil))))
}

func (s *artifactStore) decodeArtifactCursor(id, payload, token string) (artifactCursor, error) {
	if token == "" {
		return artifactCursor{phase: 1}, nil
	}
	raw, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		return artifactCursor{}, errors.New("invalid artifact cursor")
	}
	parts := strings.Split(string(raw), ":")
	if len(parts) != 5 {
		return artifactCursor{}, errors.New("invalid artifact cursor")
	}
	body := strings.Join(parts[:4], ":")
	mac := hmac.New(sha256.New, s.cursorKey)
	_, _ = mac.Write([]byte(id + "\x00" + payload + "\x00" + body))
	signature, err := hex.DecodeString(parts[4])
	if err != nil || !hmac.Equal(signature, mac.Sum(nil)) {
		return artifactCursor{}, errors.New("invalid artifact cursor")
	}
	phase, e1 := strconv.Atoi(parts[0])
	offset, e2 := strconv.ParseInt(parts[1], 10, 64)
	intra, e3 := strconv.ParseInt(parts[2], 10, 64)
	logical, e4 := strconv.ParseInt(parts[3], 10, 64)
	if e1 != nil || e2 != nil || e3 != nil || e4 != nil || phase < 1 || phase > 2 || offset < 0 || intra < 0 || logical < 0 {
		return artifactCursor{}, errors.New("invalid artifact cursor")
	}
	return artifactCursor{byte(phase), offset, intra, logical}, nil
}

func (s *artifactStore) readPage(id, payload, token string, maximum int) (map[string]any, error) {
	if maximum <= 0 {
		maximum = 64 * 1024
	}
	if maximum > 256*1024 {
		maximum = 256 * 1024
	}
	cursor, err := s.decodeArtifactCursor(id, payload, token)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	sourceActive := s.active[id] > 0
	logicalSize := s.logicalSizes[id]
	committedEnd := s.committedEnds[id]
	liveKind := s.artifactKinds[id]
	liveOrdering := s.orderings[id]
	s.active[id]++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.active[id]--
		if s.active[id] == 0 {
			delete(s.active, id)
		}
		s.mu.Unlock()
	}()
	var completion artifactCompletion
	markerData, markerErr := readFileLimited(filepath.Join(s.root, id+".complete"), 1024)
	if markerErr != nil || json.Unmarshal(markerData, &completion) != nil || completion.Size < 0 || completion.SHA256 == "" {
		sourceActive = true
	} else {
		logicalSize = completion.Size
	}
	path := filepath.Join(s.root, id+".records")
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	logicalDigest := completion.SHA256
	var text strings.Builder
	records := make([]map[string]any, 0)
	raw := make([]byte, 0, maximum)
	current := cursor
	hasMore := false
	phaseLimit := byte(2)
	if sourceActive {
		phaseLimit = 1
	}
	for phase := cursor.phase; phase <= phaseLimit; phase++ {
		startPhysical := int64(0)
		if phase == cursor.phase {
			startPhysical = cursor.offset
		}
		if _, err := file.Seek(startPhysical, io.SeekStart); err != nil {
			return nil, err
		}
		physical := startPhysical
		for {
			if physical >= committedEnd {
				break
			}
			header := make([]byte, 9)
			n, readErr := io.ReadFull(file, header)
			if readErr == io.EOF {
				break
			}
			if readErr != nil {
				return nil, fmt.Errorf("artifact_io: read header: %w", readErr)
			}
			physical += int64(n)
			length64 := int64(binary.BigEndian.Uint64(header[1:]))
			if length64 < 0 || length64 > s.itemLimit || length64 > committedEnd-physical {
				return nil, errors.New("artifact_io: invalid frame length")
			}
			frameStart := physical - int64(n)
			physical += length64
			if header[0] != phase {
				if _, readErr = file.Seek(length64, io.SeekCurrent); readErr != nil {
					return nil, fmt.Errorf("artifact_io: skip payload: %w", readErr)
				}
				continue
			}
			start := int64(0)
			if phase == cursor.phase && frameStart == cursor.offset {
				start = cursor.intra
			}
			if start > length64 {
				return nil, errors.New("invalid artifact cursor")
			}
			if start > 0 {
				if _, readErr = file.Seek(start, io.SeekCurrent); readErr != nil {
					return nil, fmt.Errorf("artifact_io: seek payload: %w", readErr)
				}
			}
			take := min(maximum-len(raw), int(length64-start))
			piece := make([]byte, take)
			if _, readErr = io.ReadFull(file, piece); readErr != nil {
				return nil, fmt.Errorf("artifact_io: read payload: %w", readErr)
			}
			remaining := length64 - start - int64(take)
			if remaining > 0 {
				if _, readErr = file.Seek(remaining, io.SeekCurrent); readErr != nil {
					return nil, fmt.Errorf("artifact_io: skip payload remainder: %w", readErr)
				}
			}
			if payload != "base64" && !utf8.Valid(piece) {
				validLength := len(piece)
				for validLength > 0 && !utf8.Valid(piece[:validLength]) {
					validLength--
				}
				if validLength == 0 {
					return nil, errors.New("artifact text page cannot form a valid UTF-8 boundary within max_bytes")
				}
				piece = piece[:validLength]
				take = validLength
			}
			raw = append(raw, piece...)
			text.Write(piece)
			if payload == "records" && take > 0 {
				records = append(records, map[string]any{"stream": map[byte]string{1: "stdout", 2: "stderr"}[phase], "data": string(piece)})
			}
			if start+int64(take) < length64 {
				current = artifactCursor{phase, frameStart, start + int64(take), cursor.logical + int64(len(raw))}
				hasMore = true
				goto done
			}
			current = artifactCursor{phase, physical, 0, cursor.logical + int64(len(raw))}
			if len(raw) == maximum {
				hasMore = true
				goto done
			}
		}
		if sourceActive {
			current = artifactCursor{phase, physical, 0, cursor.logical + int64(len(raw))}
			break
		}
		current = artifactCursor{phase + 1, 0, 0, cursor.logical + int64(len(raw))}
	}
done:
	payloadValue := map[string]any{"type": payload}
	if payload == "base64" {
		payloadValue["base64"] = base64.StdEncoding.EncodeToString(raw)
	} else if payload == "records" {
		payloadValue["records"] = records
	} else {
		payloadValue["text"] = text.String()
	}
	next := any(nil)
	if sourceActive {
		hasMore = true
	}
	if hasMore {
		next = s.encodeArtifactCursor(id, payload, current)
	}
	accessedAt := time.Now().UTC()
	_ = os.Chtimes(path, accessedAt, accessedAt)
	createdAt := completion.CreatedAt
	if createdAt.IsZero() {
		s.mu.Lock()
		createdAt = s.createdTimes[id]
		s.mu.Unlock()
	}
	if createdAt.IsZero() {
		createdAt = info.ModTime()
	}
	var digest any = logicalDigest
	kind, ordering := completion.Kind, completion.Ordering
	if kind == "" {
		kind = liveKind
	}
	if ordering == "" {
		ordering = liveOrdering
	}
	if kind == "" {
		kind = "command_output"
	}
	if ordering == "" {
		ordering = "stdout_then_stderr"
	}
	if kind == "pty" {
		for _, record := range records {
			if record["stream"] == "stdout" {
				record["stream"] = "combined"
			}
		}
	}
	warnings := []string{}
	reason := map[bool]string{true: "inline_limit", false: "none"}[hasMore]
	if sourceActive {
		digest = nil
		warnings = append(warnings, "sha256 unavailable until artifact completes")
		if cursor.logical+int64(len(raw)) >= logicalSize {
			reason = "unknown"
		}
	}
	return map[string]any{"ok": true, "artifact_id": id, "payload": payloadValue, "byte_range": map[string]any{"start": cursor.logical, "end": cursor.logical + int64(len(raw))}, "has_more": hasMore, "eof": !hasMore, "next_cursor": next, "metadata": map[string]any{"kind": kind, "mime_type": "application/x-chatrepo-command-records", "size_bytes": logicalSize, "sha256": digest, "created_at": createdAt.UTC().Format(time.RFC3339Nano), "expires_at": accessedAt.Add(s.ttl).Format(time.RFC3339Nano), "ordering": ordering}, "receipt": map[string]any{"schema_version": 1, "status": map[bool]string{true: "partial", false: "completed"}[hasMore], "completeness": map[bool]string{true: "partial", false: "complete"}[hasMore], "reason": reason, "applied": map[string]any{"source_complete": !sourceActive}, "returned": map[string]any{"bytes": len(raw)}, "total": map[string]any{"bytes": logicalSize}, "warnings": warnings}}, nil
}

func logicalArtifactMetadata(file *os.File, itemLimit int64) (int64, string, error) {
	hasher := sha256.New()
	var logicalSize int64
	for phase := byte(1); phase <= 2; phase++ {
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			return 0, "", err
		}
		for {
			var header [9]byte
			_, err := io.ReadFull(file, header[:])
			if err == io.EOF {
				break
			}
			if err != nil {
				return 0, "", fmt.Errorf("artifact_io: read metadata header: %w", err)
			}
			length := int64(binary.BigEndian.Uint64(header[1:]))
			if length < 0 || length > itemLimit {
				return 0, "", errors.New("artifact_io: invalid frame length")
			}
			if header[0] == phase {
				if _, err := io.CopyN(hasher, file, length); err != nil {
					return 0, "", fmt.Errorf("artifact_io: hash logical payload: %w", err)
				}
				logicalSize += length
			} else if _, err := file.Seek(length, io.SeekCurrent); err != nil {
				return 0, "", fmt.Errorf("artifact_io: skip metadata payload: %w", err)
			}
		}
	}
	return logicalSize, fmt.Sprintf("%x", hasher.Sum(nil)), nil
}
