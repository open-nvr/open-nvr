// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package logx is a small size-rotated log file: <file> and <file>.1, at most
// MaxBytes each, so an agent that runs for years never fills a disk.
package logx

import (
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// MaxBytes is the size at which the log rotates.
const MaxBytes = 5 << 20

// Logger writes timestamped lines to a file (and a copy to another writer).
type Logger struct {
	mu   sync.Mutex
	path string
	f    *os.File
	size int64
	echo io.Writer
}

// Open opens (or creates) the log. path "" logs to echo only.
func Open(path string, echo io.Writer) *Logger {
	l := &Logger{path: path, echo: echo}
	l.reopen()
	return l
}

func (l *Logger) reopen() {
	if l.path == "" {
		return
	}
	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	if st, err := f.Stat(); err == nil {
		l.size = st.Size()
	}
	l.f = f
}

// Printf logs one line.
func (l *Logger) Printf(format string, args ...any) {
	line := time.Now().Format("2006-01-02T15:04:05") + "  " + fmt.Sprintf(format, args...) + "\n"
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.echo != nil {
		_, _ = io.WriteString(l.echo, line)
	}
	if l.f == nil {
		return
	}
	if l.size+int64(len(line)) > MaxBytes {
		_ = l.f.Close()
		_ = os.Remove(l.path + ".1")
		_ = os.Rename(l.path, l.path+".1")
		l.size = 0
		l.reopen()
		if l.f == nil {
			return
		}
	}
	n, _ := l.f.WriteString(line)
	l.size += int64(n)
}

// Close closes the file.
func (l *Logger) Close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f != nil {
		_ = l.f.Close()
		l.f = nil
	}
}
