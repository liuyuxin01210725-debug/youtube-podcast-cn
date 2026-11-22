"""Bounded, offline EPUB/XHTML import for private book packages.

Not an EPUB renderer. All manifest XHTML is inventoried (including non-spine
notes); unsafe inputs are rejected, unsupported content remains explicit.
No network, extraction to disk, OCR, decryption, or model invocation.
"""
from __future__ import annotations
import hashlib
import json
import posixpath
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from studio_books import build_book, narrator_document, read_user_text

PARSER_VERSION = 'shengjian-bounded-epub-1.2'
MAX_TOTAL = 64 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
BLOCKS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'dt', 'dd', 'blockquote', 'pre', 'table', 'aside'}
EPUB_TYPE = '{http://www.idpf.org/2007/ops}type'

class BookImportError(ValueError): pass

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def tag(e): return e.tag.rsplit('}', 1)[-1] if isinstance(e.tag, str) else ''
def text(e):
    def inline(node):
        value=node.text or ''
        for child in node:
            structural=tag(child) in BLOCKS | {'div','section','article','ul','ol','dl','tr','td','th'}
            value += ('\n' if tag(child)=='br' else (' '+inline(child)+' ' if structural else inline(child))) + (child.tail or '')
        return value
    return re.sub(r'\s+', ' ', inline(e)).strip()

def safe_name(name):
    name = unquote(name)
    if not name or '\\' in name or ':' in name or name.startswith('/') or '..' in PurePosixPath(name).parts:
        raise BookImportError('Unsafe archive/resource path')
    return name

def resolve(base, href):
    u = urlsplit(href)
    if u.scheme or u.netloc:
        return {'external': href}
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(u.path))) if u.path else base
    safe_name(path)
    return {'href': path, 'fragment': unquote(u.fragment)}

def xml(data):
    if len(data) > MAX_XML or re.search(br'<!ENTITY|<!DOCTYPE[^>]*(?:SYSTEM|PUBLIC|\[)', data, re.I):
        raise BookImportError('External entities / oversized XML are unsupported')
    try: return ET.fromstring(data)
    except ET.ParseError as e: raise BookImportError('Invalid XHTML/XML') from e

def resource_blocks(href, root):
    body = next((e for e in root.iter() if tag(e) == 'body'), None)
    if body is None: raise BookImportError('XHTML resource has no body')
    blocks, unresolved = [], []
    parents={child:parent for parent in body.iter() for child in parent}
    def in_navigation(e):
        while e is not None:
            if tag(e)=='nav':return True
            e=parents.get(e)
        return False
    for e in body.iter():
        if in_navigation(e):continue
        t = tag(e)
        parent=parents.get(e)
        while parent is not None and tag(parent) not in BLOCKS:parent=parents.get(parent)
        simple_note_paragraph=parent is not None and tag(parent)=='aside' and t=='p' and len(parent)==1 and parent[0] is e and not e.get('id') and not (parent.text or '').strip() and not (e.tail or '').strip()
        if t in BLOCKS and parent is not None and not simple_note_paragraph:
            unresolved.append({'href':href,'fragment':e.get('id'),'kind':'nested_block_layout','reason':'nested block/table/list structure is flattened; paragraph/relationship rendering not independently verified'})
        if t in {'script', 'iframe', 'object', 'embed'} or any(k.lower().startswith('on') for k in e.attrib):
            raise BookImportError('Active XHTML is unsupported')
        if t in {'td','th'} and (e.get('rowspan','1')!='1' or e.get('colspan','1')!='1'):
            unresolved.append({'href':href,'fragment':e.get('id'),'kind':'spanning_table_cell','reason':'merged-cell relationships not verified'})
        if t in {'ol','dl','pre','sup','sub'} or e.get('hidden') is not None or e.get('style'):
            unresolved.append({'href':href,'fragment':e.get('id'),'kind':t,'reason':'layout/numbering/visibility semantics not verified by bounded importer'})
        if set(e.get(EPUB_TYPE,'').split()) & {'footnote','endnote'} and t not in BLOCKS:
            unresolved.append({'href':href,'fragment':e.get('id'),'kind':'note_container','reason':'non-block note wrapper not supported; text retained as appendix'})
        if t in {'img', 'svg', 'math', 'audio', 'video', 'canvas'}:
            unresolved.append({'href': href, 'fragment': e.get('id'), 'kind': t, 'reason': 'not translated or narrated; no OCR/interpretation', 'alt': e.get('alt', '')})
    def add(e, path, kind, value):
        if not value.strip(): return
        anchor = {'href': href, 'dom_path': path, 'fragment': e.get('id')}
        refs = [resolve(href, a.get('href')) for a in e.iter() if tag(a) == 'a' and a.get('href')]
        blocks.append({'id': 'b-' + digest([anchor, kind])[:24], 'kind': kind, 'text': value, 'source_anchor': anchor, 'references': refs, 'source_text_hash': digest(value)})
    def walk(e, path):
        t = tag(e)
        if t == 'nav': return
        if t in BLOCKS:
            kind = 'footnote' if set(e.get(EPUB_TYPE, '').split()) & {'footnote', 'endnote'} or e.get('role') in {'doc-footnote', 'doc-endnote'} else t
            if t == 'table':
                rows = [' | '.join(text(c) for c in row if tag(c) in {'td', 'th'}) for row in e.iter() if tag(row) == 'tr']
                value = '\n'.join(r for r in rows if r)
                caption = next((text(c) for c in e if tag(c) == 'caption'), '')
                value = (caption + '\n' if caption else '') + value
            else: value = text(e)
            add(e, path, kind, value)
            return
        if e.text and e.text.strip(): add(e, path + '/text()', 'text', e.text.strip())
        counts = {}
        for child in e:
            name = tag(child); counts[name] = counts.get(name, 0) + 1
            walk(child, f'{path}/{name}[{counts[name]}]')
            if child.tail and child.tail.strip(): add(e, f'{path}/{name}[{counts[name]}]/tail()', 'text', child.tail.strip())
    walk(body, '/html/body')
    return blocks, unresolved

def import_source(path: Path, *, title='', rights=None):
    path = Path(path)
    rights = rights or {}
    if rights.get('kind') not in {'self-authored', 'permission', 'public-domain'} or not rights.get('statement') or not rights.get('region'):
        raise BookImportError('Explicit lawful-source statement and applicable region required')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL:
        raise BookImportError('Source must be a bounded regular file')
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    unresolved, resources, chapters, all_blocks = [], [], [], []
    if path.suffix.lower() in {'.txt', '.md'}:
        body = read_user_text(path)
        # Existing scaffold is reused only for explicitly simple plain-text input.
        if re.search(r'!\[|<[^>]+>|\[\^|\$\$|^\s*\|', body, re.M):
            raise BookImportError('Rich Markdown is unsupported; use bounded XHTML EPUB')
        book = build_book(body, title=title or path.stem, language='en')
        for chapter in book['chapters']:
            blocks = []
            for n, b in enumerate(chapter['blocks']):
                anchor = {'href': path.name, 'dom_path': f"text/{chapter['id']}/{n}", 'fragment': None}
                blocks.append({'id': 'b-'+digest(anchor)[:24], 'kind': 'text', 'text': b['text'], 'source_anchor': anchor, 'references': [], 'source_text_hash': digest(b['text'])})
            chapters.append({'id': chapter['id'], 'title': chapter['title'], 'blocks': blocks})
        all_blocks = [b for c in chapters for b in c['blocks']]
        resources = [{'href': path.name, 'kind': 'plain-text', 'block_ids': [b['id'] for b in all_blocks]}]
        source_title = book['title']
    elif path.suffix.lower() == '.epub':
        try:
            with zipfile.ZipFile(path) as z:
                names, total = set(), 0
                if len(z.infolist()) > 2048: raise BookImportError('Too many ZIP entries')
                for info in z.infolist():
                    safe_name(info.filename)
                    if info.filename in names or stat.S_ISLNK(info.external_attr >> 16): raise BookImportError('Duplicate or symlink ZIP entry')
                    names.add(info.filename); total += info.file_size
                    if info.flag_bits & 1 or info.file_size > 16*1024*1024 or info.file_size > max(1, info.compress_size)*200: raise BookImportError('Encrypted/oversized archive entry')
                if total > MAX_TOTAL: raise BookImportError('Archive expansion limit')
                if 'META-INF/encryption.xml' in names:
                    raise BookImportError('Encrypted/obfuscated EPUB resources unsupported; no DRM diagnosis or removal')
                if z.read('mimetype').strip() != b'application/epub+zip': raise BookImportError('Not an EPUB')
                container = xml(z.read('META-INF/container.xml'))
                roots = [e.get('full-path') for e in container.iter() if tag(e) == 'rootfile']
                if len(roots) != 1: raise BookImportError('Multiple renditions unsupported')
                opf_name = safe_name(roots[0]); opf = xml(z.read(opf_name))
                source_title = next((text(e) for e in opf.iter() if tag(e) == 'title'), path.stem)
                source_language = next((text(e) for e in opf.iter() if tag(e)=='language'), '')
                if not source_language.lower().startswith('en'):raise BookImportError('This importer requires declared English source language')
                manifest = [e for e in opf.iter() if tag(e) == 'item']
                by_id = {}
                for e in manifest:
                    resolved = resolve(opf_name, e.get('href', ''))
                    if 'external' in resolved or not e.get('id') or e.get('id') in by_id: raise BookImportError('Invalid manifest ID/resource')
                    href = resolved['href']
                    if href not in names: raise BookImportError('Missing manifest resource: '+href)
                    by_id[e.get('id')] = {'href': href, 'media_type': e.get('media-type'), 'properties': e.get('properties', '')}
                spine = [e.get('idref') for e in opf.iter() if tag(e) == 'itemref']
                if not spine or len(spine) != len(set(spine)) or any(k not in by_id for k in spine): raise BookImportError('Invalid spine')
                order = spine + [k for k in by_id if k not in spine]
                seen_resources = set()
                for k in order:
                    item = by_id[k]; href = item['href']
                    if href in seen_resources: raise BookImportError('Aliased manifest resources unsupported')
                    seen_resources.add(href)
                    if item['media_type'] in {'application/xhtml+xml', 'text/html'}:
                        root = xml(z.read(href)); blocks, issues = resource_blocks(href, root)
                        unresolved.extend(issues); all_blocks.extend(blocks)
                        resources.append({**item, 'spine_index': spine.index(k) if k in spine else None, 'block_ids': [b['id'] for b in blocks]})
                        if blocks:
                            heading = next((b['text'] for b in blocks if b['kind'] in {'h1', 'h2'}), href)
                            chapters.append({'id': 'c-'+digest(href)[:20], 'title': heading, 'navigation_basis': 'OPF resource order; document-based listening chapters', 'blocks': blocks})
                    else:
                        resources.append({**item, 'block_ids': []})
                        if item['media_type']=='text/css':unresolved.append({'href':href,'kind':'stylesheet','reason':'generated/hidden text semantics not verified; no remote CSS fetched'})
                        if item['media_type'] not in {'text/css', 'application/x-dtbncx+xml', 'font/otf', 'font/ttf', 'application/font-woff', 'font/woff', 'font/woff2'}:
                            unresolved.append({'href': href, 'kind': item['media_type'], 'reason': 'non-text resource not verified; preserved in source'})
                # Rehome referenced non-spine footnotes to their first referring chapter.
                # Keep original resource/DOM anchors. No text is dropped or duplicated.
                owners = {}
                for c in chapters:
                    for b in c['blocks']:
                        for r in b['references']:
                            if 'href' in r: owners.setdefault((r['href'], r.get('fragment', '')), c['id'])
                moved = {}
                for c in chapters:
                    keep = []
                    for b in c['blocks']:
                        anchor = b['source_anchor']; target = owners.get((anchor['href'], anchor.get('fragment') or ''))
                        if b['kind'] == 'footnote' and target and target != c['id']: moved.setdefault(target, []).append(b)
                        else: keep.append(b)
                    c['blocks'] = keep
                for c in chapters: c['blocks'].extend(moved.get(c['id'], []))
                chapters = [c for c in chapters if c['blocks']]
                known = {(b['source_anchor']['href'], b['source_anchor'].get('fragment')) for b in all_blocks}
                # Internal fragment IDs may be inline, not block roots. Inventory them too.
                fragments = set()
                for item in by_id.values():
                    if item['media_type'] in {'application/xhtml+xml', 'text/html'}:
                        fragments.update((item['href'], e.get('id')) for e in xml(z.read(item['href'])).iter() if e.get('id'))
                for b in all_blocks:
                    for r in b['references']:
                        if 'href' in r and (r['href'] not in seen_resources or r.get('fragment') and (r['href'], r['fragment']) not in fragments):
                            unresolved.append({'kind': 'unresolved_reference', 'source_block_id': b['id'], 'target': r})
        except (zipfile.BadZipFile, KeyError) as e: raise BookImportError('Invalid/incomplete EPUB') from e
    else: raise BookImportError('Unsupported format; only bounded EPUB or simple UTF-8 txt/md')
    if not all_blocks: raise BookImportError('No readable text')
    ids = [b['id'] for c in chapters for b in c['blocks']]
    if len(ids) != len(set(ids)) or set(ids) != {b['id'] for b in all_blocks}: raise BookImportError('Coverage inventory mismatch')
    inventory = {'resources': resources, 'expected_block_ids': ids, 'unresolved': unresolved, 'expected_units': len(ids)+len(unresolved), 'readable_text_units': len(ids), 'scope': 'whole_source_file; publication completeness not independently verified'}
    revision = digest([PARSER_VERSION, raw_hash, inventory, chapters])
    result = {'schema_version': 1, 'title': title or source_title, 'language': 'en', 'source_byte_hash': raw_hash, 'source_revision': raw_hash, 'extraction_revision': revision, 'parser_version': PARSER_VERSION, 'rights': rights, 'chapters': chapters, 'inventory': inventory}
    # Reuse the existing single-narrator document scaffold, never its placeholder times as alignment.
    result['narrator_document'] = narrator_document(result)
    return result
