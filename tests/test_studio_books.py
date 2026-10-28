import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_books import EN_MAX, ZH_MAX, build_book, narrator_document, read_user_text, resume_fields, seek


def _zh_chapter_book():
    paragraph = '这是用来填满一块听书的句子。' * 40
    chunk = '\n\n'.join([paragraph] * 8)
    return '第一章 开端\n\n' + chunk + '\n\n第二章 后来\n\n' + chunk


class BookScaffoldTests(unittest.TestCase):
    def test_long_text_is_chapters_and_blocks_not_one_file(self):
        book = build_book(_zh_chapter_book(), title='示例', language='zh')
        blocks = [block for chapter in book['chapters'] for block in chapter['blocks']]
        self.assertEqual([chapter['title'] for chapter in book['chapters']], ['第一章 开端', '第二章 后来'])
        self.assertGreaterEqual(len(blocks), 4)
        self.assertTrue(all(block['measure'] <= ZH_MAX for block in blocks))
        self.assertFalse(book['whole_book_file'])
        self.assertEqual(book['listen_unit'], 'chapter_block')
        self.assertEqual(book['diarization'], 'skipped')
        self.assertEqual(book['expected_speakers'], 1)
        self.assertEqual(book['narrator'], {'speaker_id': 'narrator', 'voice_slot': 'male_1'})
        self.assertNotEqual(blocks[0]['text'], _zh_chapter_book())
        cursor = resume_fields(book)
        self.assertEqual(cursor['chapter_id'], 'c001')
        self.assertEqual(cursor['block_id'], 'c001b001')
        self.assertEqual(cursor['offset_chars'], 0)
        document = narrator_document(book)
        self.assertEqual(len(document['segments']), len(blocks))
        self.assertEqual({segment['speaker_id'] for segment in document['segments']}, {'narrator'})
        self.assertEqual(document['speakers']['narrator']['voice_slot'], 'male_1')
        self.assertFalse(document['voices_confirmed'])
        self.assertTrue(document['segments'][0]['chinese'])

    def test_no_heading_still_splits_a_long_book_and_a_short_note_stays_one_block(self):
        long_book = build_book('字' * 10000, language='zh')
        blocks = [block for chapter in long_book['chapters'] for block in chapter['blocks']]
        self.assertEqual(len(long_book['chapters']), 1)
        self.assertGreaterEqual(len(blocks), 2)
        self.assertTrue(all(block['measure'] <= ZH_MAX for block in blocks))
        short = build_book('只有一段很短的话。', title='短', language='zh')
        self.assertEqual(len(short['chapters'][0]['blocks']), 1)

    def test_english_chapters_use_a_word_budget(self):
        words = ' '.join(['word'] * 4000)
        text = 'Chapter 1 Start\n\n' + words + '\n\nChapter 2 Next\n\n' + words
        book = build_book(text, language='en')
        blocks = [block for chapter in book['chapters'] for block in chapter['blocks']]
        self.assertEqual([chapter['title'] for chapter in book['chapters']], ['Chapter 1 Start', 'Chapter 2 Next'])
        self.assertGreaterEqual(len(blocks), 2)
        self.assertTrue(all(block['measure'] <= EN_MAX for block in blocks))
        document = narrator_document(book)
        self.assertEqual(document['segments'][0]['chinese'], '')
        self.assertIn('word', document['segments'][0]['text'])

    def test_resume_cursor_rejects_a_missing_block_and_a_bare_url(self):
        book = build_book('只有一段很短的话。', language='zh')
        moved = seek(book, chapter_id='c001', block_id='c001b001', offset_chars=2)
        self.assertEqual(moved['offset_chars'], 2)
        self.assertEqual(resume_fields(book)['offset_chars'], 2)
        with self.assertRaisesRegex(ValueError, '找不到'):
            seek(book, chapter_id='c009', block_id='c009b001', offset_chars=0)
        with self.assertRaisesRegex(ValueError, '超出'):
            seek(book, chapter_id='c001', block_id='c001b001', offset_chars=10_000)
        with self.assertRaisesRegex(ValueError, '不会从链接抓书'):
            build_book('https://example.com/pirate.epub', language='zh')
        kept = build_book('正文里提到 https://example.com/note 但不是抓来的。\n\n下一段。', language='zh')
        self.assertEqual(kept['chapters'][0]['blocks'][0]['id'], 'c001b001')

    def test_reader_rejects_binary_and_remote_paths(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'book.txt'
            path.write_bytes('你好'.encode())
            self.assertIn('你好', read_user_text(path))
            binary = Path(root) / 'book.bin'
            binary.write_bytes(b'abc\0def')
            with self.assertRaisesRegex(ValueError, '纯文本'):
                read_user_text(binary)


if __name__ == '__main__':
    unittest.main()
