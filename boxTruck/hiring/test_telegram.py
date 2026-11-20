import io
import json
from unittest import mock

import requests
from django.test import SimpleTestCase
from PIL import Image

from hiring import telegram as tg


def resp(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


def ok(result):
    return resp(200, {"ok": True, "result": result})


def err(status, desc, **params):
    body = {"ok": False, "error_code": status, "description": desc}
    if params:
        body["parameters"] = params
    return resp(status, body)


def jpeg(size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(buf, "JPEG")
    return buf.getvalue()


class TokenAndChatTests(SimpleTestCase):
    @mock.patch("hiring.telegram.requests.post")
    def test_check_token(self, post):
        post.return_value = ok({"id": 1, "username": "lead_bot"})
        self.assertEqual(tg.check_token("123:abc"), "lead_bot")
        self.assertTrue(post.call_args[0][0].endswith("/bot123:abc/getMe"))

    @mock.patch("hiring.telegram.requests.post")
    def test_check_token_rejected(self, post):
        post.return_value = err(401, "Unauthorized")
        with self.assertRaises(tg.TelegramError) as cm:
            tg.check_token("bad")
        self.assertEqual(str(cm.exception), "Unauthorized")
        self.assertEqual(post.call_count, 1)

    def test_check_token_empty(self):
        with self.assertRaises(tg.TelegramError):
            tg.check_token("  ")

    @mock.patch("hiring.telegram.requests.post")
    def test_get_chat(self, post):
        post.side_effect = [ok({"title": "Leads", "type": "supergroup"}), ok(7)]
        self.assertEqual(
            tg.get_chat("t", "-100123456"),
            {"title": "Leads", "type": "supergroup", "members": 7},
        )

    @mock.patch("hiring.telegram.requests.post")
    def test_get_chat_member_count_failure_is_none(self, post):
        post.side_effect = [ok({"title": "Leads", "type": "group"}), err(400, "nope")]
        self.assertIsNone(tg.get_chat("t", "-100123456")["members"])

    @mock.patch("hiring.telegram.requests.post")
    def test_get_chat_not_found(self, post):
        post.return_value = err(400, "Bad Request: chat not found")
        with self.assertRaises(tg.TelegramError) as cm:
            tg.get_chat("t", "-1")
        self.assertEqual(str(cm.exception), "Bad Request: chat not found")

    @mock.patch("hiring.telegram.requests.post")
    def test_non_json_error(self, post):
        post.return_value = resp(403)
        with self.assertRaises(tg.TelegramError) as cm:
            tg.check_token("t")
        self.assertEqual(str(cm.exception), "HTTP 403")

    @mock.patch("hiring.telegram.requests.post")
    def test_token_not_logged(self, post):
        post.return_value = err(400, "bad")
        with self.assertLogs("hiring.telegram", "ERROR") as logs:
            with self.assertRaises(tg.TelegramError):
                tg.check_token("999:SECRET")
        self.assertNotIn("SECRET", "\n".join(logs.output))


class CaptionTests(SimpleTestCase):
    def build(self, **kw):
        base = dict(
            full_name="Tyler Jenkins",
            phone="8175550198",
            dispatcher_name="Sardor Aliyev",
            dispatcher_username="sardor_aliyev",
            link="https://x.test/hiring?driver=123",
            notes=[],
        )
        base.update(kw)
        return tg.build_caption(**base)

    def test_full(self):
        self.assertEqual(
            self.build(),
            "<b>New lead</b>\nName: Tyler Jenkins\nPhone: +1 (817) 555-0198\n"
            "Dispatcher: @sardor_aliyev (Sardor Aliyev)\n"
            '<a href="https://x.test/hiring?driver=123">Open in Box Truck Manage</a>',
        )

    def test_no_username(self):
        self.assertIn("Dispatcher: Sardor Aliyev\n", self.build(dispatcher_username=""))

    def test_username_with_at(self):
        self.assertIn("Dispatcher: @sardor_aliyev (", self.build(dispatcher_username="@sardor_aliyev"))

    def test_unassigned(self):
        c = self.build(dispatcher_name=None, dispatcher_username=None)
        self.assertIn("Dispatcher: not assigned, the team is empty", c)

    def test_phone_formats(self):
        self.assertIn("+1 (817) 555-0198", self.build(phone="18175550198"))
        self.assertIn("+1 (817) 555-0198", self.build(phone="(817) 555-0198"))
        self.assertIn("Phone: +44 20 7946", self.build(phone="+44 20 7946"))
        self.assertIn("Phone: \n", self.build(phone=None))

    def test_notes(self):
        c = self.build(notes=["No CDL uploaded", "No cargo space photo"])
        self.assertTrue(c.endswith("\nNo CDL uploaded · No cargo space photo"))

    def test_notes_string_and_empty(self):
        self.assertTrue(self.build(notes="No side photo").endswith("\nNo side photo"))
        self.assertNotIn("·", self.build(notes=None))
        self.assertEqual(self.build(notes=[]).count("\n"), 4)

    def test_escaping(self):
        c = self.build(
            full_name="<b>Bob</b> & Co",
            dispatcher_name="A <i>",
            link='https://x.test/?a=1&b="2"',
            notes=["x < y"],
        )
        self.assertIn("Name: &lt;b&gt;Bob&lt;/b&gt; &amp; Co", c)
        self.assertIn("(A &lt;i&gt;)", c)
        self.assertIn('href="https://x.test/?a=1&amp;b=&quot;2&quot;"', c)
        self.assertIn("x &lt; y", c)


class SendTests(SimpleTestCase):
    @mock.patch("hiring.telegram.requests.post")
    def test_no_photos_send_message(self, post):
        post.return_value = ok({"message_id": 11})
        out = tg.send_lead("t", 55, [], "cap")
        self.assertEqual(out, {"message_id": "11", "attempts": 1})
        self.assertTrue(post.call_args[0][0].endswith("/sendMessage"))
        d = post.call_args[1]["data"]
        self.assertEqual(d["text"], "cap")
        self.assertEqual(d["parse_mode"], "HTML")
        self.assertEqual(d["chat_id"], "55")

    @mock.patch("hiring.telegram.requests.post")
    def test_one_photo(self, post):
        post.return_value = ok({"message_id": 12})
        out = tg.send_lead("t", "-1001", [b"img"], "cap")
        self.assertEqual(out["message_id"], "12")
        self.assertTrue(post.call_args[0][0].endswith("/sendPhoto"))
        kw = post.call_args[1]
        self.assertEqual(kw["data"]["caption"], "cap")
        self.assertEqual(kw["data"]["parse_mode"], "HTML")
        self.assertEqual(kw["files"]["photo"][1], b"img")
        self.assertEqual(kw["timeout"], (5, 30))

    @mock.patch("hiring.telegram.requests.post")
    def test_album(self, post):
        post.return_value = ok([{"message_id": 20}, {"message_id": 21}, {"message_id": 22}])
        out = tg.send_lead("t", "-1001", [b"a", b"b", b"c"], "cap")
        self.assertEqual(out, {"message_id": "20", "attempts": 1})
        self.assertTrue(post.call_args[0][0].endswith("/sendMediaGroup"))
        kw = post.call_args[1]
        media = json.loads(kw["data"]["media"])
        self.assertEqual([m["media"] for m in media], ["attach://photo0", "attach://photo1", "attach://photo2"])
        self.assertEqual(media[0]["caption"], "cap")
        self.assertEqual(media[0]["parse_mode"], "HTML")
        self.assertNotIn("caption", media[1])
        self.assertNotIn("caption", media[2])
        self.assertTrue(all(m["type"] == "photo" for m in media))
        self.assertEqual(set(kw["files"]), {"photo0", "photo1", "photo2"})
        self.assertEqual(kw["files"]["photo1"][1], b"b")

    @mock.patch("hiring.telegram.requests.post")
    def test_two_photos_album(self, post):
        post.return_value = ok([{"message_id": 5}, {"message_id": 6}])
        tg.send_lead("t", "1", [b"a", b"b"], "c")
        self.assertEqual(len(json.loads(post.call_args[1]["data"]["media"])), 2)

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_retry_after_429(self, post, sleep):
        post.side_effect = [err(429, "Too Many Requests", retry_after=3), ok({"message_id": 1})]
        out = tg.send_lead("t", "1", [], "c")
        self.assertEqual(out["attempts"], 2)
        sleep.assert_called_once_with(3.0)

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_retry_after_capped(self, post, sleep):
        post.side_effect = [err(429, "slow", retry_after=500), ok({"message_id": 1})]
        tg.send_lead("t", "1", [], "c")
        sleep.assert_called_once_with(30.0)

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_retry_5xx_backoff(self, post, sleep):
        post.side_effect = [err(502, "Bad Gateway"), err(500, "oops"), ok({"message_id": 9})]
        out = tg.send_lead("t", "1", [b"x"], "c")
        self.assertEqual(out, {"message_id": "9", "attempts": 3})
        self.assertEqual([c[0][0] for c in sleep.call_args_list], [2, 5])

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_retry_network_error(self, post, sleep):
        post.side_effect = [requests.ConnectionError("boom"), requests.Timeout("t"), ok({"message_id": 2})]
        out = tg.send_lead("t", "1", [], "c")
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(sleep.call_count, 2)

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_no_retry_on_400(self, post, sleep):
        post.return_value = err(400, "Bad Request: chat not found")
        with self.assertLogs("hiring.telegram", "ERROR"):
            with self.assertRaises(tg.TelegramError) as cm:
                tg.send_lead("t", "1", [], "c")
        self.assertEqual(str(cm.exception), "Bad Request: chat not found")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_gives_up_after_three(self, post, sleep):
        post.return_value = err(502, "Bad Gateway")
        with self.assertLogs("hiring.telegram", "ERROR") as logs:
            with self.assertRaises(tg.TelegramError) as cm:
                tg.send_lead("999:SECRET", "1", [], "c")
        self.assertEqual(str(cm.exception), "Bad Gateway")
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("SECRET", "\n".join(logs.output))

    @mock.patch("hiring.telegram.requests.post")
    def test_send_text(self, post):
        post.return_value = ok({"message_id": 77})
        self.assertEqual(tg.send_text("t", "-1", "<b>Hi</b>"), "77")
        d = post.call_args[1]["data"]
        self.assertEqual((d["text"], d["parse_mode"]), ("<b>Hi</b>", "HTML"))

    @mock.patch("hiring.telegram.time.sleep")
    @mock.patch("hiring.telegram.requests.post")
    def test_send_text_error(self, post, sleep):
        post.return_value = err(403, "Forbidden: bot was kicked")
        with self.assertRaises(tg.TelegramError) as cm:
            tg.send_text("t", "-1", "x")
        self.assertEqual(str(cm.exception), "Forbidden: bot was kicked")


class ToPhotoTests(SimpleTestCase):
    def open(self, data):
        img = Image.open(io.BytesIO(data))
        img.load()
        return img

    def test_pdf(self):
        buf = io.BytesIO()
        Image.new("RGB", (300, 400), (0, 120, 0)).save(buf, "PDF")
        out = tg.to_photo(buf.getvalue(), "cdl.pdf", "application/pdf")
        self.assertIsNotNone(out)
        img = self.open(out)
        self.assertEqual(img.format, "JPEG")
        self.assertLessEqual(max(img.size), 2560)
        self.assertGreater(img.height, img.width)

    def test_pdf_detected_by_magic(self):
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(buf, "PDF")
        self.assertIsNotNone(tg.to_photo(buf.getvalue(), "scan", None))

    def test_png_alpha(self):
        buf = io.BytesIO()
        Image.new("RGBA", (50, 50), (255, 0, 0, 0)).save(buf, "PNG")
        out = tg.to_photo(buf.getvalue(), "a.png", "image/png")
        img = self.open(out)
        self.assertEqual((img.format, img.mode), ("JPEG", "RGB"))
        px = img.getpixel((10, 10))
        self.assertTrue(all(c > 240 for c in px))

    def test_large_jpeg_downscaled(self):
        out = tg.to_photo(jpeg((4000, 1000)), "big.jpg", "image/jpeg")
        img = self.open(out)
        self.assertEqual(img.size[0], 2560)
        self.assertEqual(img.size[1], 640)

    def test_small_jpeg_kept_size(self):
        img = self.open(tg.to_photo(jpeg((40, 30)), "s.jpg", None))
        self.assertEqual(img.size, (40, 30))

    def test_exif_orientation(self):
        im = Image.new("RGB", (60, 20), "blue")
        exif = Image.Exif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        im.save(buf, "JPEG", exif=exif)
        img = self.open(tg.to_photo(buf.getvalue(), "r.jpg", None))
        self.assertEqual(img.size, (20, 60))

    def test_garbage(self):
        self.assertIsNone(tg.to_photo(b"not a picture at all", "x.jpg", "image/jpeg"))
        self.assertIsNone(tg.to_photo(b"", "x.jpg", None))
        self.assertIsNone(tg.to_photo(b"%PDF-1.4 garbage", "x.pdf", "application/pdf"))
