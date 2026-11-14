"""비밀 파일을 읽기 전에 차단하고 정확한 소스와 patch만 다룬다."""
import re
from pathlib import Path, PurePosixPath
from .profiles import digest, manifest_image

ALLOWED = {'.py','.java','.js','.json','.xml','.properties','.sql','.html','.css','.lock','.toml','.md','.txt'}
DENIED = re.compile(r'(secret|private|credential|oauth|password|token|auth|dump|conftest)', re.I)
SENSITIVE = re.compile(r'-----BEGIN .*PRIVATE KEY|(?:password|secret|token)\s*[:=]\s*[\"\'][^\"\'\n]{6,}[\"\']', re.I)


def safe_path(root, relative):
    if not isinstance(relative, str) or '\\' in relative or '\x00' in relative:
        raise ValueError('unsafe_path')
    p = PurePosixPath(relative)
    if p.is_absolute() or not p.parts or any(x in ('.','..') or x.startswith('.') or DENIED.search(x) for x in p.parts):
        raise ValueError('sensitive_or_unsafe_path')
    if p.suffix.lower() not in ALLOWED and p.name != 'Dockerfile':
        raise ValueError('extension_not_allowed')
    root = Path(root).resolve()
    out = root.joinpath(*p.parts)
    if any(x.is_symlink() for x in [out, *list(out.parents)[:len(p.parts)-1]]):
        raise ValueError('symlink_forbidden')
    if not out.resolve().is_relative_to(root):
        raise ValueError('path_escape')
    return out


def source_files(root):
    root = Path(root)
    result = {}
    def walk(directory):
        for p in sorted(directory.iterdir()):
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                raise ValueError('symlink_forbidden')
            if p.name.startswith('.') or rel == 'target' or p.name in ('node_modules','__pycache__') or DENIED.search(p.name):
                continue
            if p.is_dir():
                walk(p)
            else:
                try:
                    safe_path(root, rel)
                except ValueError:
                    continue
                data = p.read_bytes()
                if len(data) > 512000:
                    raise ValueError('source_too_large')
                text = data.decode('utf-8')
                if SENSITIVE.search(text):
                    raise ValueError('sensitive_source_content')
                result[rel] = text
    walk(root)
    return result


def snapshot(root, profile, image, proof):
    manifest_image(image, profile['image_repository'])
    if not proof or proof.get('image') != image or proof.get('profile_id') != profile['id']:
        raise ValueError('source_proof_required')
    files = source_files(root)
    hashes = {path:digest(text.encode()) for path,text in files.items()}
    if not hashes or hashes != proof.get('file_sha256'):
        raise ValueError('source_mismatch')
    commit = proof.get('base_commit')
    if commit is not None and not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('invalid_source_commit')
    result = {'base_commit':commit,'base_image':image,'files':files,'file_sha256':hashes,
              'profile_id':profile['id'],'affected_services':profile['affected_services'],'provenance_verified':True}
    result['id'] = digest(result)
    return result


def map_stack(location, profile, source_snapshot):
    path = re.sub(r':\d+(?::\d+)?$', '', location)
    prefix = profile['runtime_root'] + '/'
    if path.startswith(prefix):
        path = path[len(prefix):]
    elif path.startswith('/'):
        raise ValueError('stack_root_mismatch')
    if profile['language'] == 'java' and path.endswith('.java') and '/' not in path:
        candidates = [p for p in source_snapshot['files'] if p.startswith('src/main/java/') and p.endswith('/'+path)]
        if len(candidates) != 1:
            raise ValueError('ambiguous_java_source')
        path = candidates[0]
    if path not in source_snapshot['files']:
        raise ValueError('stack_source_unverified')
    return path


def parse_patch(diff, root, test_root):
    if not isinstance(diff,str) or len(diff)>1000000:
        raise ValueError('patch_invalid')
    lines = diff.splitlines(keepends=True)
    changes = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith('--- '):
            raise ValueError('patch_header_required')
        old = lines[i][4:].strip()
        i += 1
        if i >= len(lines) or not lines[i].startswith('+++ '):
            raise ValueError('patch_header_required')
        new = lines[i][4:].strip()
        i += 1
        if not new.startswith('b/') or (old != '/dev/null' and old != 'a/'+new[2:]):
            raise ValueError('rename_delete_forbidden')
        name = new[2:]
        path = safe_path(root, name)
        if name in changes:
            raise ValueError('duplicate_patch_file')
        if old == '/dev/null':
            if path.exists():
                raise ValueError('new_file_exists')
            original = []
        else:
            original = path.read_text().splitlines(keepends=True)
        output = []
        cursor = 0
        hunks = 0
        while i < len(lines) and lines[i].startswith('@@ '):
            m = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?', lines[i])
            if not m:
                raise ValueError('invalid_hunk')
            start,count,newstart,newcount = int(m[1]),int(m[2] or 1),int(m[3]),int(m[4] or 1)
            offset = max(0,start-1)
            if offset < cursor or offset > len(original):
                raise ValueError('hunk_offset')
            output.extend(original[cursor:offset])
            cursor = offset
            i += 1
            consumed = produced = 0
            while i < len(lines) and not lines[i].startswith(('@@ ','--- ')):
                line = lines[i]
                if not line or line[0] not in ' +-':
                    raise ValueError('invalid_hunk_line')
                if line[0] in ' -':
                    if cursor >= len(original) or original[cursor] != line[1:]:
                        raise ValueError('patch_context_mismatch')
                    cursor += 1
                    consumed += 1
                if line[0] in ' +':
                    output.append(line[1:])
                    produced += 1
                i += 1
            if consumed != count or produced != newcount:
                raise ValueError('hunk_count_mismatch')
            hunks += 1
        if not hunks:
            raise ValueError('missing_hunk')
        output.extend(original[cursor:])
        text = ''.join(output)
        if SENSITIVE.search(text):
            raise ValueError('sensitive_patch_content')
        changes[name] = text
    if not changes:
        raise ValueError('empty_patch')
    return changes


def apply_changes(root, changes, tests_only, test_root):
    for name,text in changes.items():
        is_test = name.startswith(test_root+'/')
        if tests_only != is_test:
            continue
        path = safe_path(root,name)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text)
