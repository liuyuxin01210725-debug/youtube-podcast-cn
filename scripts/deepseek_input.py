"""Count the pinned V4 text-only chat template locally, without truncation."""
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import zipfile


ARCHIVE = Path(__file__).resolve().parents[1] / 'data/deepseek/deepseek_v4_tokenizer.zip'
ARCHIVE_SHA256 = 'e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495'


@lru_cache(maxsize=1)
def _load(archive):
    import jinja2
    import tokenizers

    if (tokenizers.__version__, jinja2.__version__) != ('0.23.3', '3.1.6'):
        raise ValueError('Unverified tokenizer runtime')
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('Unverified V4 tokenizer archive')
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        config = json.loads(files.read('deepseek_v4_tokenizer/tokenizer_config.json'))
        tokenizer = tokenizers.Tokenizer.from_buffer(files.read('deepseek_v4_tokenizer/tokenizer.json'))
    if tokenizer.truncation is not None or tokenizer.padding is not None:
        raise ValueError('Unexpected tokenizer configuration')
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return tokenizer, env.from_string(config['chat_template']), config['bos_token']['content']


def count(messages):
    if (not isinstance(messages, list) or len(messages) != 2
            or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                   or not isinstance(m['content'], str) or m['role'] != role
                   for m, role in zip(messages, ('system', 'user')))):
        raise ValueError('Unsupported message framing')
    tokenizer, template, bos = _load(ARCHIVE.read_bytes())
    rendered = template.render(messages=messages, bos_token=bos, add_generation_prompt=True)
    return len(tokenizer.encode(rendered, add_special_tokens=False).ids)
