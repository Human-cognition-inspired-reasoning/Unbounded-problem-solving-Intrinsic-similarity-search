"""Deterministic attachment preparation. No LLM summaries or prompt concatenation."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from files.errors import AttachmentError

FORMATS = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
           '.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv', '.tsv': 'text/tab-separated-values',
           '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}
PREPARATION_VERSION = 'attachments-v1'
_pdf_lock = threading.Lock()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode_text(data):
    for encoding in ('utf-8-sig', 'utf-16' if data[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8', 'cp949'):
        try:
            text = data.decode(encoding)
            if '\x00' in text:
                break
            return text
        except UnicodeError:
            continue
    raise AttachmentError('텍스트 인코딩을 읽을 수 없습니다. UTF-8로 저장해 주세요.')


def validate_office(data, suffix, embedded=False):
    # Embedded chart workbooks are validated as ZIP/XML, never offered as uploads.
    expected = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml'}[suffix]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if expected not in archive.namelist() or len(entries) > 5000 or sum(x.file_size for x in entries) > 100 * 1024**2:
                raise AttachmentError('Office 파일 형식 또는 압축 해제 크기가 허용 범위를 벗어났습니다.')
            for entry in entries:
                name = entry.filename.lower()
                if 'vbaproject' in name:
                    raise AttachmentError('매크로가 있는 문서는 지원하지 않습니다.')
                if '/embeddings/' in name and not entry.is_dir():
                    if not embedded and name.endswith('.xlsx'):
                        validate_office(archive.read(entry), '.xlsx', embedded=True)
                    else:
                        raise AttachmentError('지원하지 않는 삽입 파일이 있습니다. PDF로 저장해 첨부해 주세요.')
                if name.endswith('.rels'):
                    for rel in ElementTree.fromstring(archive.read(entry)):
                        if rel.attrib.get('TargetMode') == 'External' and not rel.attrib.get('Type', '').endswith('/hyperlink'):
                            raise AttachmentError('외부 파일을 불러오는 연결이 있습니다. 내용을 파일 안에 포함해 저장해 주세요.')
    except (zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException, KeyError) as exc:
        raise AttachmentError('올바른 Office 파일이 아닙니다.') from exc


def csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def check_table(rows, config):
    if not rows or not any(any(cell != '' for cell in row) for row in rows):
        raise AttachmentError('표가 비어 있습니다.')
    if len(rows) > config['ATTACHMENT_MAX_ROWS']:
        raise AttachmentError(f"표는 헤더 포함 {config['ATTACHMENT_MAX_ROWS']}행까지 전달할 수 있습니다. 범위를 줄여 주세요.")
    if max(map(len, rows)) > 128 or sum(map(len, rows)) > 50000:
        raise AttachmentError('표는 최대 128열, 50,000셀까지 지원합니다. 범위를 줄여 주세요.')


def text_pdf(text, config):
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font = config.get('ATTACHMENT_FONT_PATH') or next((p for p in (
        '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
    ) if Path(p).is_file()), '')
    if not font:
        raise AttachmentError('텍스트 PDF 변환용 글꼴이 설정되지 않았습니다.', 'CONVERTER_UNAVAILABLE', 503)
    # ReportLab's font registry is process-global.
    with _pdf_lock:
        font_name = 'AttachmentText-' + hashlib.sha256(font.encode()).hexdigest()[:12]
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, font))
        out = io.BytesIO()
        pdf = canvas.Canvas(out, pagesize=(595, 842), invariant=True)
        pdf.setFont(font_name, 10)
        y, pages = 800, 1
        for raw in text.splitlines() or ['']:
            # Preserve Markdown source, including syntax; no HTML or remote images.
            raw = raw.expandtabs(4)
            lines = [raw]
            for line in lines:
                # Character wrapping also handles unbroken CJK strings and code tokens.
                chunks, current = [], ''
                for char in line:
                    if current and pdfmetrics.stringWidth(current + char, font_name, 10) > 515:
                        chunks.append(current)
                        current = ''
                    current += char
                chunks.append(current)
                for chunk in chunks:
                    if y < 42:
                        pages += 1
                        if pages > config['ATTACHMENT_MAX_PAGES']:
                            raise AttachmentError('PDF로 변환한 텍스트가 페이지 한도를 초과합니다.')
                        pdf.showPage()
                        pdf.setFont(font_name, 10)
                        y = 800
                    pdf.drawString(40, y, chunk)
                    y -= 15
        pdf.save()
        return out.getvalue()


def office_pdf(data, suffix, config):
    validate_office(data, suffix)
    executable = config.get('LIBREOFFICE_BIN') or shutil.which('soffice')
    if not executable:
        raise AttachmentError('문서 변환기가 설치되지 않았습니다. PDF로 저장해 첨부해 주세요.', 'CONVERTER_UNAVAILABLE', 503)
    with tempfile.TemporaryDirectory(prefix='coscientist-office-') as temp:
        root = Path(temp)
        source = root / ('input' + suffix)
        source.write_bytes(data)
        profile = root / 'profile'
        profile.mkdir()
        (profile / 'user').mkdir()
        (profile / 'user/registrymodifications.xcu').write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>')
        child_env = os.environ.copy()
        if sys.platform == 'darwin' and not child_env.get('FONTCONFIG_FILE'):
            # Headless macOS builds can see only their bundled Latin fonts.
            # Scope font discovery/cache to this conversion, not the user's settings.
            from xml.sax.saxutils import escape
            config_path = root / 'fonts.conf'
            directories = ['/System/Library/Fonts', '/Library/Fonts', str(Path.home() / 'Library/Fonts')]
            config_path.write_text('<?xml version="1.0"?><fontconfig>' +
                                  ''.join('<dir>' + escape(p) + '</dir>' for p in directories) +
                                  '<cachedir>' + escape(str(root / 'font-cache')) + '</cachedir></fontconfig>')
            child_env['FONTCONFIG_FILE'] = str(config_path)
        try:
            result = subprocess.run([
                executable, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--nologo',
                '--nodefault', '--norestore', '--convert-to', 'pdf', '--outdir', temp, str(source),
            ], capture_output=True, timeout=config['ATTACHMENT_CONVERSION_TIMEOUT'], check=False, env=child_env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AttachmentError('문서 변환에 실패했습니다. PDF로 저장해 다시 첨부해 주세요.') from exc
        target = root / 'input.pdf'
        if result.returncode or not target.is_file():
            raise AttachmentError('문서를 PDF로 변환하지 못했습니다. PDF로 저장해 첨부해 주세요.')
        if target.stat().st_size > config['ATTACHMENT_MAX_DERIVED_BYTES']:
            raise AttachmentError('변환된 PDF가 파일 크기 한도를 초과합니다.')
        return target.read_bytes()


def describe_pdf(data, config):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachmentError('암호가 걸린 PDF는 지원하지 않습니다.')
        pages = len(reader.pages)
        if not 1 <= pages <= config['ATTACHMENT_MAX_PAGES']:
            raise AttachmentError(f"PDF는 {config['ATTACHMENT_MAX_PAGES']}페이지까지 지원합니다.")
        # A conservative, explicitly approximate budget; actual provider usage settles it.
        tokens = sum(max(4000, math.ceil(len((page.extract_text() or '').encode('utf-8')) / 2) + 2000)
                     for page in reader.pages)
        return {'pages': pages, 'estimatedTokens': tokens}
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError('읽을 수 없는 PDF입니다.') from exc


def prepare(data, filename, config):
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise AttachmentError('지원하지 않는 파일 형식입니다.')
    stem = Path(filename).stem
    files = []
    if suffix in ('.pdf', '.docx', '.txt', '.md'):
        if suffix in ('.txt', '.md'):
            data = text_pdf(decode_text(data), config)
        elif suffix != '.pdf':
            data = office_pdf(data, suffix, config)
        files.append({'name': stem + '.pdf', 'mime': 'application/pdf', 'data': data, **describe_pdf(data, config)})
    elif suffix in ('.png', '.jpg', '.jpeg'):
        from PIL import Image, ImageOps
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ('PNG', 'JPEG') or source.width * source.height > 20_000_000:
                    raise AttachmentError('PNG·JPEG 이미지는 최대 2천만 픽셀까지 지원합니다.')
                expected = 'PNG' if suffix == '.png' else 'JPEG'
                if source.format != expected:
                    raise AttachmentError('이미지 확장자와 실제 형식이 다릅니다.')
                source.load()
                im = ImageOps.exif_transpose(source)
                im.thumbnail((2048, 2048))
                out = io.BytesIO()
                if expected == 'JPEG':
                    im.convert('RGB').save(out, format='JPEG', quality=90)
                else:
                    im.save(out, format='PNG')
                files.append({'name': stem + ('.png' if expected == 'PNG' else '.jpg'),
                              'mime': FORMATS[suffix], 'data': out.getvalue(), 'estimatedTokens': 6000,
                              'width': im.width, 'height': im.height})
        except AttachmentError:
            raise
        except Exception as exc:
            raise AttachmentError('읽을 수 없는 이미지입니다.') from exc
    elif suffix in ('.csv', '.tsv'):
        try:
            text = decode_text(data)
            csv.field_size_limit(1024 * 1024)
            dialect = csv.excel_tab if suffix == '.tsv' else csv.excel
            rows = []
            for row in csv.reader(io.StringIO(text, newline=''), dialect=dialect, strict=True):
                rows.append(row)
                if len(rows) > config['ATTACHMENT_MAX_ROWS']:
                    break
            check_table(rows, config)
            encoded = csv_bytes(rows)
        except csv.Error as exc:
            raise AttachmentError('CSV 구분자·따옴표 또는 셀 크기를 확인해 주세요.') from exc
        files.append({'name': stem + '.csv', 'mime': 'text/csv', 'data': encoded, 'rows': len(rows),
                      'estimatedTokens': max(1, math.ceil(len(encoded) / 2))})
    else:
        raise AttachmentError('지원하지 않는 파일 형식입니다.')
    if sum(len(f['data']) for f in files) > config['ATTACHMENT_MAX_DERIVED_BYTES']:
        raise AttachmentError('변환된 파일 크기가 한도를 초과합니다.')
    return files
