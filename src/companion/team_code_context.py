"""Small local engineering excerpts. Trusted configured root only; never execute code.

Returned text is untrusted source context, not instructions or native authority.
Hashes identify the bytes read, not a promise that the working tree stays unchanged.
"""
import hashlib
import json
import os
import re
import stat
from itertools import islice
from pathlib import Path

_FILE_BYTES = 64 * 1024
_TOTAL_BYTES = 2 * 1024 * 1024
_FILES = 512
_ENTRIES = 4096
_OUTPUT_BYTES = 3500
_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.html', '.swift', '.m', '.h', '.md'}
_EXCLUDED = {'data', 'transcript', 'transcripts', 'sessions', 'node_modules', '__pycache__',
             'build', 'dist', 'vendor'}
_STOP = set('a an the how what where when why does do is are was were can could would should '
            'i me my we our you your it this that these those to of on in for from and or with '
            'please tell explain show about work works implementation code kyra hello hi hey '
            'thanks thank morning evening good xin chào chao'.split())
_DIR_FLAGS = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _words(text):
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    return set(re.findall(r'[^\W\d_][^\W_]*', text.casefold()))


def _open_dir(root):
    # Walk from / without resolve(): even symlinks in root ancestors are refused.
    path = Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid code root')
    fd = os.open('/', _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_file(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], getattr(os, "O_RDONLY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
    finally:
        os.close(fd)


def lookup(root: Path | None, question: str) -> dict:
    """Read src/web/scripts/apple and docs/feature-guide.md, never user paths.

    Limits: 16 lexical terms, 4096 directory entries, depth 12, 512 files,
    64 KiB/file, 2 MiB total reads, five eight-line/600-byte excerpts and
    3500 UTF-8 JSON bytes. Symlinks and multiply linked files are refused.
    No settings/model imports, subprocesses, network, writes or dispatch. None
    disables lookup. A greeting with no useful terms performs no filesystem I/O.
    Status/counters describe this bounded read, not complete repository coverage.
    """
    result = dict(status='disabled', matches=[], files_read=0, bytes_read=0, truncated=False)
    if root is None:
        return result
    if not isinstance(question, str) or len(question) > 16000:
        result['status'] = 'skipped'
        return result
    terms = sorted(_words(question) - _STOP)[:16]
    if not terms:
        result['status'] = 'skipped'
        return result
    terms = set(terms)
    try:
        root_fd = _open_dir(root)
    except (OSError, ValueError, TypeError):
        result['status'] = 'unavailable'
        return result
    candidates = [('docs', 'feature-guide.md')]
    entries_left = _ENTRIES

    def collect(fd, prefix):
        nonlocal entries_left
        if len(prefix) > 12 or entries_left <= 0:
            result['truncated'] = True
            return
        with os.scandir(fd) as scan:
            entries = list(islice(scan, entries_left + 1))
        if len(entries) > entries_left:
            # An oversized directory is omitted, not sampled in filesystem order.
            entries_left = 0
            result['truncated'] = True
            return
        entries_left -= len(entries)
        for entry in sorted(entries, key=lambda item: item.name):
            name = entry.name
            if (name.startswith('.') or name.casefold() in _EXCLUDED
                    or any(ord(c) < 32 for c in name)):
                continue
            parts = (*prefix, name)
            if len('/'.join(parts).encode('utf-8', errors='replace')) > 240:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                    try:
                        collect(child, parts)
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False) and Path(name).suffix in _SUFFIXES:
                    candidates.append(parts)
            except OSError:
                result['truncated'] = True

    ranked = []
    try:
        for directory in ('src', 'web', 'scripts', 'apple'):
            try:
                child = os.open(directory, _DIR_FLAGS, dir_fd=root_fd)
            except OSError:
                continue
            try:
                collect(child, (directory,))
            finally:
                os.close(child)
        # Filename matches are read first so a bounded scan can find named modules.
        priority = {'docs': 0, 'src': 1, 'web': 2, 'scripts': 3, 'apple': 4}
        candidates.sort(key=lambda p: (-len(terms & _words('/'.join(p))), priority[p[0]], p))
        for parts in candidates:
            if result['files_read'] >= _FILES or result['bytes_read'] >= _TOTAL_BYTES:
                result['truncated'] = True
                break
            try:
                fd = _open_file(root_fd, parts)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    if before.st_nlink > 1:
                        result['truncated'] = True
                        continue
                    if before.st_size > _FILE_BYTES or before.st_size > _TOTAL_BYTES - result['bytes_read']:
                        result['truncated'] = True
                        continue
                    raw = os.read(fd, min(_FILE_BYTES, _TOTAL_BYTES - result['bytes_read']))
                    result['files_read'] += 1
                    result['bytes_read'] += len(raw)
                    after = os.fstat(fd)
                    if (after.st_nlink > 1 or before.st_size != len(raw) or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ctime_ns != after.st_ctime_ns):
                        result['truncated'] = True
                        continue
                finally:
                    os.close(fd)
                text = raw.decode('utf-8')
                if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                    continue
                lines = text.splitlines()
                if not lines:
                    continue
                filename = '/'.join(parts)
                path_score = len(terms & _words(filename))
                scores = [len(terms & _words(line)) for line in lines]
                score = max(scores)
                if not score and not path_score:
                    continue
                index = scores.index(score)
                start = max(0, index - 1)
                if len(lines[start].encode('utf-8')) > 200:
                    start = index
                excerpt = '\n'.join(lines[start:start + 8]).encode('utf-8')[:600].decode('utf-8', errors='ignore')
                ranked.append((-(2 * score + path_score), filename, dict(
                    filename=filename, line=start + 1, sha256=hashlib.sha256(raw).hexdigest(), excerpt=excerpt)))
            except (OSError, UnicodeError):
                result['truncated'] = True
    finally:
        os.close(root_fd)
    for _, _, match in sorted(ranked):
        if len(result['matches']) >= 5:
            result['truncated'] = True
            break
        result['matches'].append(match)
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > _OUTPUT_BYTES - 16:
            result['matches'].pop()
            result['truncated'] = True
            break
    result['status'] = 'ok' if result['matches'] else 'no_matches'
    return result
