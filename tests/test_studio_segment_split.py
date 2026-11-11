#!/usr/bin/env python3
"""Official document split path: stable ids, timeline, translation and render contracts."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from studio_document import (
    next_segment_id,
    render_contract_errors,
    split_document_segment,
)
from studio_server import Handler, Studio, ThreadingHTTPServer, read, write


def _segment(ident, start, end, text, chinese, speaker, **extra):
    row = {
        "id": ident,
        "start_ms": start,
        "end_ms": end,
        "text": text,
        "chinese": chinese,
        "speaker_id": speaker,
        "ambiguous": False,
        "is_ad": False,
    }
    row.update(extra)
    return row


def _doc(segments):
    speakers = {segment["speaker_id"]: {"voice_slot": "male_1" if segment["speaker_id"].endswith("00") or segment["speaker_id"] == "SPEAKER_00" else "male_2"} for segment in segments}
    if "SPEAKER_00" in speakers:
        speakers["SPEAKER_00"]["voice_slot"] = "male_1"
    if "SPEAKER_01" in speakers:
        speakers["SPEAKER_01"]["voice_slot"] = "male_2"
    return {
        "schema_version": 1,
        "segments": segments,
        "speakers": speakers,
        "references": {},
        "voices_confirmed": True,
    }


# Draft cuts from evidence/支持路径诊断-2026-11-11.md, using the real Russ texts.
RUSS_MIXED = {
    "seg-000142": {
        "start_ms": 856760,
        "end_ms": 870440,
        "text": "present, where they may actually engage more manually with their work in order to build those capacities within themselves. Well, and so I want to go back because there's something interesting there and it's not related to AI, it's",
        "chinese": "存在，他们实际上可能更多地手动参与工作，以便在自己内部建立那些能力。那么，我想回到前面，因为那里有些有趣的东西，而且它与AI无关，它",
        "left_text": "present, where they may actually engage more manually with their work in order to build those capacities within themselves.",
        "right_text": "Well, and so I want to go back because there's something interesting there and it's not related to AI, it's",
        "left_chinese": "存在，他们实际上可能更多地手动参与工作，以便在自己内部建立那些能力。",
        "right_chinese": "那么，我想回到前面，因为那里有些有趣的东西，而且它与AI无关，它",
        "left_speaker": "SPEAKER_00",
        "right_speaker": "SPEAKER_01",
        "cut_ms": 863600,
    },
    "seg-000144": {
        "start_ms": 883100,
        "end_ms": 895380,
        "text": "to Lean Pub to publish your last two books? I mean, what did that let you do that you couldn't do with a publisher? I mean, I'd love to say it's because I met Len, the person that runs Lean Pub, and he's one of the most lovely human beings",
        "chinese": "选择Lean Pub来出版你最近两本书？我的意思是，它让你能做到什么出版商做不到的事？我是说，我很想说是因为我遇到了Len，Lean Pub的运营者，他是最可爱的人之一",
        "left_text": "to Lean Pub to publish your last two books? I mean, what did that let you do that you couldn't do with a publisher?",
        "right_text": "I mean, I'd love to say it's because I met Len, the person that runs Lean Pub, and he's one of the most lovely human beings",
        "left_chinese": "选择Lean Pub来出版你最近两本书？我的意思是，它让你能做到什么出版商做不到的事？",
        "right_chinese": "我是说，我很想说是因为我遇到了Len，Lean Pub的运营者，他是最可爱的人之一",
        "left_speaker": "SPEAKER_01",
        "right_speaker": "SPEAKER_00",
        "cut_ms": 889240,
    },
}


class SplitLogicTests(unittest.TestCase):
    def test_left_keeps_id_right_gets_next_stable_id_others_unchanged(self):
        document = _doc([
            _segment("seg-000141", 800000, 856760, "prev", "前", "SPEAKER_01"),
            _segment("seg-000142", 856760, 870440, "left right", "左 右", "SPEAKER_00"),
            _segment("seg-000143", 870440, 882680, "next", "后", "SPEAKER_01"),
        ])
        neighbor_ids_before = ["seg-000141", "seg-000143"]
        updated, left_id, right_id = split_document_segment(document, {
            "id": "seg-000142",
            "cut_ms": 863600,
            "left": {"text": "left", "chinese": "左", "speaker_id": "SPEAKER_00"},
            "right": {"text": "right", "chinese": "右", "speaker_id": "SPEAKER_01"},
        })
        ids = [segment["id"] for segment in updated["segments"]]
        self.assertEqual(left_id, "seg-000142")
        self.assertEqual(right_id, "seg-000144")
        self.assertEqual(ids, ["seg-000141", "seg-000142", "seg-000144", "seg-000143"])
        self.assertEqual([segment["id"] for segment in updated["segments"] if segment["id"] in neighbor_ids_before], neighbor_ids_before)
        self.assertEqual(updated["segments"][0]["text"], "prev")
        self.assertEqual(updated["segments"][3]["text"], "next")
        left, right = updated["segments"][1], updated["segments"][2]
        self.assertEqual((left["start_ms"], left["end_ms"]), (856760, 863600))
        self.assertEqual((right["start_ms"], right["end_ms"]), (863600, 870440))
        self.assertEqual(left["speaker_id"], "SPEAKER_00")
        self.assertEqual(right["speaker_id"], "SPEAKER_01")
        self.assertTrue(left["ambiguous"] and right["ambiguous"])
        self.assertEqual(render_contract_errors(updated), [])

    def test_next_id_skips_existing_and_supports_three_way_split(self):
        document = _doc([
            _segment(
                "seg-000469",
                2796740,
                2800980,
                "My favorite agent though, if I may, mine too, is the Avocados di Aboli.",
                "不过我最喜欢的智能体，如果我可以这么说的话，我也是，是 Avocados di Aboli。",
                "SPEAKER_00",
            ),
            _segment("seg-000470", 2800980, 2810000, "after", "之后", "SPEAKER_01"),
        ])
        first, _, mid_id = split_document_segment(document, {
            "id": "seg-000469",
            "cut_ms": 2798200,
            "left": {
                "text": "My favorite agent though, if I may,",
                "chinese": "不过我最喜欢的智能体，如果我可以这么说的话，",
                "speaker_id": "SPEAKER_00",
            },
            "right": {
                "text": "mine too, is the Avocados di Aboli.",
                "chinese": "我也是，是 Avocados di Aboli。",
                "speaker_id": "SPEAKER_01",
            },
        })
        self.assertEqual(mid_id, "seg-000471")
        second, _, last_id = split_document_segment(first, {
            "id": mid_id,
            "cut_ms": 2799000,
            "left": {"text": "mine too,", "chinese": "我也是，", "speaker_id": "SPEAKER_01"},
            "right": {"text": "is the Avocados di Aboli.", "chinese": "是 Avocados di Aboli。", "speaker_id": "SPEAKER_00"},
        })
        ids = [segment["id"] for segment in second["segments"]]
        self.assertEqual(ids, ["seg-000469", "seg-000471", "seg-000472", "seg-000470"])
        self.assertEqual(last_id, "seg-000472")
        self.assertEqual(
            [(s["start_ms"], s["end_ms"]) for s in second["segments"][:3]],
            [(2796740, 2798200), (2798200, 2799000), (2799000, 2800980)],
        )
        self.assertEqual(second["segments"][3]["id"], "seg-000470")
        self.assertEqual(render_contract_errors(second), [])

    def test_rejects_invented_speaker_bad_cut_and_text_that_does_not_rejoin(self):
        document = _doc([_segment("seg-000001", 0, 1000, "hello world", "你好 世界", "SPEAKER_00")])
        with self.assertRaisesRegex(ValueError, "不能新增说话人"):
            split_document_segment(document, {
                "id": "seg-000001", "cut_ms": 400,
                "left": {"text": "hello", "chinese": "你好", "speaker_id": "SPEAKER_00"},
                "right": {"text": "world", "chinese": "世界", "speaker_id": "SPEAKER_99"},
            })
        with self.assertRaisesRegex(ValueError, "切点必须落在原段起止之间"):
            split_document_segment(document, {
                "id": "seg-000001", "cut_ms": 0,
                "left": {"text": "hello", "chinese": "你好", "speaker_id": "SPEAKER_00"},
                "right": {"text": "world", "chinese": "世界", "speaker_id": "SPEAKER_00"},
            })
        with self.assertRaisesRegex(ValueError, "左右英文必须拼回原段原文"):
            split_document_segment(document, {
                "id": "seg-000001", "cut_ms": 400,
                "left": {"text": "hello", "chinese": "你好", "speaker_id": "SPEAKER_00"},
                "right": {"text": "there", "chinese": "世界", "speaker_id": "SPEAKER_00"},
            })
        with self.assertRaisesRegex(ValueError, "左右译文必须拼回原段中文"):
            split_document_segment(document, {
                "id": "seg-000001", "cut_ms": 400,
                "left": {"text": "hello", "chinese": "你好", "speaker_id": "SPEAKER_00"},
                "right": {"text": "world", "chinese": "别人", "speaker_id": "SPEAKER_00"},
            })

    def test_russ_draft_cuts_preserve_unsplit_neighbors_and_render_contract(self):
        segments = [
            _segment("seg-000141", 840000, 856760, "before", "之前", "SPEAKER_01"),
            _segment("seg-000142", RUSS_MIXED["seg-000142"]["start_ms"], RUSS_MIXED["seg-000142"]["end_ms"], RUSS_MIXED["seg-000142"]["text"], RUSS_MIXED["seg-000142"]["chinese"], "SPEAKER_00"),
            _segment("seg-000143", 870440, 883100, "middle", "中间保留的六十四处之一", "SPEAKER_01"),
            _segment("seg-000144", RUSS_MIXED["seg-000144"]["start_ms"], RUSS_MIXED["seg-000144"]["end_ms"], RUSS_MIXED["seg-000144"]["text"], RUSS_MIXED["seg-000144"]["chinese"], "SPEAKER_00"),
            _segment("seg-000145", 895380, 900000, "after", "之后", "SPEAKER_00"),
        ]
        document = _doc(segments)
        updated = document
        for ident, draft in RUSS_MIXED.items():
            updated, _, _ = split_document_segment(updated, {
                "id": ident,
                "cut_ms": draft["cut_ms"],
                "left": {"text": draft["left_text"], "chinese": draft["left_chinese"], "speaker_id": draft["left_speaker"]},
                "right": {"text": draft["right_text"], "chinese": draft["right_chinese"], "speaker_id": draft["right_speaker"]},
            })
        by_id = {segment["id"]: segment for segment in updated["segments"]}
        self.assertEqual(by_id["seg-000143"]["chinese"], "中间保留的六十四处之一")
        self.assertEqual(by_id["seg-000141"]["text"], "before")
        self.assertEqual(by_id["seg-000145"]["text"], "after")
        self.assertIn("seg-000146", by_id)  # first new id after 145
        self.assertEqual(render_contract_errors(updated), [])
        # Translation contract: existing ids keep their chinese; new ids have provided chinese.
        self.assertTrue(by_id["seg-000142"]["chinese"].startswith("存在"))
        self.assertTrue(any(segment["id"] not in {"seg-000141", "seg-000142", "seg-000143", "seg-000144", "seg-000145"} and segment["chinese"] for segment in updated["segments"]))

    def test_next_segment_id_from_highest(self):
        self.assertEqual(next_segment_id([{"id": "seg-000587"}, {"id": "seg-000001"}]), "seg-000588")


class SplitApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(self.tmp.name)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.studio = self.studio
        self.metadata_patch = patch("studio_server.lookup_source_metadata", return_value=None)
        self.topic_patch = patch("studio_server.lookup_chinese_topic", return_value=None)
        self.metadata_patch.start()
        self.topic_patch.start()
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_port

    def tearDown(self):
        self.studio.stop = True
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()
        self.topic_patch.stop()
        self.metadata_patch.stop()

    def request(self, path, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"X-CSRF-Token": self.studio.token}
        conn.request("POST" if data is not None else "GET", path, json.dumps(data).encode() if data is not None else None, headers)
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        return status, json.loads(body.decode()) if body else {}

    def test_split_endpoint_writes_document_marks_stale_and_document_post_still_requires_full_list(self):
        ident = self.studio.create([{"kind": "youtube", "value": "https://youtu.be/abc"}], {"action": "prepare"})[0]
        self.studio.action(ident, "cancel")
        path = self.studio.job(ident)
        document = _doc([
            _segment("seg-000001", 0, 1000, "aaa bbb", "甲 乙", "SPEAKER_00"),
            _segment("seg-000002", 1000, 2000, "ccc", "丙", "SPEAKER_01"),
        ])
        write(path / "document.json", document)
        (path / "output.mp3").write_bytes(b"audio")
        self.studio.migrate_assets(path)
        self.assertFalse(self.studio.info(path)["stale"])
        status, body = self.request(f"/api/jobs/{ident}/split-segment", {
            "id": "seg-000001",
            "cut_ms": 400,
            "left": {"text": "aaa", "chinese": "甲", "speaker_id": "SPEAKER_00"},
            "right": {"text": "bbb", "chinese": "乙", "speaker_id": "SPEAKER_01"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["left_id"], "seg-000001")
        self.assertEqual(body["right_id"], "seg-000003")
        written = read(path / "document.json")
        self.assertEqual([segment["id"] for segment in written["segments"]], ["seg-000001", "seg-000003", "seg-000002"])
        self.assertTrue(self.studio.info(path)["stale"])
        # Existing document POST still rejects a length mismatch (cannot sneak inserts).
        self.assertEqual(self.request(f"/api/jobs/{ident}/document", {"segments": [
            {"id": "seg-000001", "chinese": "甲", "speaker_id": "SPEAKER_00"},
        ]})[0], 400)
        # After split, a legal save must include every current id.
        self.assertEqual(self.request(f"/api/jobs/{ident}/document", {"segments": [
            {"id": segment["id"], "chinese": segment["chinese"], "speaker_id": segment["speaker_id"]}
            for segment in written["segments"]
        ]})[0], 200)

    def test_split_rejected_while_job_is_queued(self):
        ident = self.studio.create([{"kind": "youtube", "value": "https://youtu.be/abc"}], {"action": "prepare"})[0]
        path = self.studio.job(ident)
        write(path / "document.json", _doc([_segment("seg-000001", 0, 1000, "aaa bbb", "甲 乙", "SPEAKER_00")]))
        status, body = self.request(f"/api/jobs/{ident}/split-segment", {
            "id": "seg-000001",
            "cut_ms": 400,
            "left": {"text": "aaa", "chinese": "甲", "speaker_id": "SPEAKER_00"},
            "right": {"text": "bbb", "chinese": "乙", "speaker_id": "SPEAKER_00"},
        })
        self.assertEqual(status, 400)
        self.assertIn("等待任务停止", body.get("error", ""))
