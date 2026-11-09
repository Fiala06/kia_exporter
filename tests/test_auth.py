"""Tests for the login / re-auth plumbing in kia_influx.py.

Run from the repo root:  python -m unittest discover -s tests
Nothing here talks to Kia or a mailbox; both are faked.
"""
import os
import sys
import pickle
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.update({
    "KIA_USERNAME": "driver@example.com",
    "KIA_PASSWORD": "hunter2",
    "KIA_PIN": "1234",
    "TOKEN_FILE": os.path.join(TMP, "kia_token.pkl"),
    "MPG_STATE_FILE": os.path.join(TMP, "mpg_state.json"),
    "OTP_IMAP_HOST": "imap.example.com",
    "OTP_IMAP_USER": "kia-codes@example.com",
    "OTP_IMAP_PASS": "app-password",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kia_influx as ki  # noqa: E402
from hyundai_kia_connect_api.ApiImpl import OTPRequest  # noqa: E402
from hyundai_kia_connect_api.Token import Token  # noqa: E402
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE  # noqa: E402
from hyundai_kia_connect_api.exceptions import AuthenticationOTPRequired  # noqa: E402


def otp_request(**kw):
    base = dict(request_id="xid", otp_key="key", has_email=True, has_sms=True,
                email="d***@example.com", sms="***1234")
    base.update(kw)
    return OTPRequest(**base)


def make_token(sid="sid-1", rm="rm-1"):
    return Token(username="driver@example.com", password="hunter2",
                 access_token=sid, refresh_token=rm, device_id="DEV", pin="1234")


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for OtpMailbox."""
    mailbox = {}          # uid (bytes) -> raw RFC822 bytes
    opened = 0

    def __init__(self, host, port):
        FakeIMAP.opened += 1

    def login(self, user, pw):
        return "OK", [b""]

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(sorted(self.mailbox))]
        if cmd == "FETCH":
            uid = args[0]
            return "OK", [(b"1 (UID %s RFC822 {n})" % uid, self.mailbox[uid]), b")"]
        raise AssertionError(cmd)

    def logout(self):
        return "BYE", [b""]


HTML_MAIL = (b"From: Kia Connect <noreply@kia.com>\r\n"
             b"Subject: Your Kia verification request\r\n"
             b"Content-Type: text/html; charset=utf-8\r\n\r\n"
             b"<html><body><p>Hi,</p><p>Your one-time code is "
             b"<b>482913</b>. It expires in 20 minutes.</p>"
             b"<p>Kia America, Inc. 111 Peters Canyon Rd, Irvine CA 92606</p>"
             b"</body></html>\r\n")
NEWSLETTER = (b"From: Kia <news@kia.com>\r\nSubject: Spring service offers\r\n"
              b"Content-Type: text/plain\r\n\r\nSave on tires this month!\r\n")
SUBJECT_MAIL = (b"From: noreply@kia.com\r\nSubject: 775512 is your Kia code\r\n"
                b"Content-Type: text/plain\r\n\r\nUse it within 20 minutes.\r\n")


class MailboxTests(unittest.TestCase):
    def setUp(self):
        FakeIMAP.mailbox = {}
        self.box = ki.OtpMailbox(poll=0, imap_factory=FakeIMAP)

    def test_extract_code_default_regex(self):
        self.assertEqual(ki.extract_code("code is 123456 ok"), "123456")
        self.assertIsNone(ki.extract_code("zip 92606 and 1234567 long"))
        self.assertIsNone(ki.extract_code(None))

    def test_only_mail_that_arrives_after_the_request_counts(self):
        FakeIMAP.mailbox[b"10"] = HTML_MAIL          # an OLD code mail
        before = self.box.snapshot()
        self.assertEqual(before, {b"10"})
        self.assertIsNone(self.box.wait_for_code(before, timeout=0))

    def test_finds_code_in_html_body_and_skips_newsletters(self):
        before = self.box.snapshot()
        FakeIMAP.mailbox[b"11"] = NEWSLETTER
        FakeIMAP.mailbox[b"12"] = HTML_MAIL
        self.assertEqual(self.box.wait_for_code(before, timeout=0), "482913")

    def test_prefers_subject(self):
        FakeIMAP.mailbox[b"20"] = SUBJECT_MAIL
        self.assertEqual(self.box.wait_for_code(set(), timeout=0), "775512")

    def test_strip_html(self):
        self.assertEqual(ki.strip_html("<p>a &amp; <b>b</b></p>").split(), ["a", "&", "b"])


class FakeManager:
    """Stands in for VehicleManager: login -> OTP -> verify."""
    def __init__(self, login_result="otp", renew_result="token"):
        self.token = None
        self.vehicles = {}
        self.sent = []
        self.verified = []
        self._login_result = login_result
        self.api = mock.Mock()
        self.api.login.side_effect = self._api_login
        self._renew_result = renew_result
        self.otp_request = None

    def login(self):
        if self._login_result == "otp":
            self.otp_request = otp_request()
            return self.otp_request
        self.token = make_token()
        self.vehicles = {"v1": mock.Mock(name="Sportage")}
        return True

    def send_otp(self, method):
        self.sent.append(method)

    def verify_otp_and_complete_login(self, code):
        self.verified.append(code)
        self.token = make_token(sid="sid-new", rm="rm-new")
        v = mock.Mock(); v.name = "Sportage"
        self.vehicles = {"v1": v}

    def _api_login(self, **kw):
        if self._renew_result == "otp":
            return otp_request()
        return make_token(sid="sid-renewed", rm=kw["token"].refresh_token)

    def initialize_vehicles(self):
        v = mock.Mock(); v.name = "Sportage"
        self.vehicles["v1"] = v


class LoginTests(unittest.TestCase):
    def setUp(self):
        FakeIMAP.mailbox = {}
        if os.path.exists(ki.TOKEN_FILE):
            os.remove(ki.TOKEN_FILE)
        ki._last_reauth = None
        ki._token_seen = None
        ki._token_saved = None
        self.imap_patch = mock.patch.object(ki.imaplib, "IMAP4_SSL", FakeIMAP)
        self.imap_patch.start()
        self.addCleanup(self.imap_patch.stop)

    def test_auto_otp_login_end_to_end(self):
        fake = FakeManager()

        def arrive(method):
            fake.sent.append(method)
            FakeIMAP.mailbox[b"1"] = HTML_MAIL   # Kia's mail lands
        fake.send_otp = arrive
        with mock.patch.object(ki, "new_manager", return_value=fake), \
                mock.patch.object(ki, "OTP_WAIT_SECONDS", 1):
            mgr = ki.auto_otp_login()
        self.assertIs(mgr, fake)
        self.assertEqual(fake.sent, [OTP_NOTIFY_TYPE.EMAIL])
        self.assertEqual(fake.verified, ["482913"])
        with open(ki.TOKEN_FILE, "rb") as f:
            self.assertEqual(pickle.load(f).access_token, "sid-new")

    def test_auto_otp_login_times_out_without_mail(self):
        fake = FakeManager()
        with mock.patch.object(ki, "new_manager", return_value=fake), \
                mock.patch.object(ki, "OTP_WAIT_SECONDS", 0):
            with self.assertRaises(RuntimeError):
                ki.auto_otp_login()
        self.assertFalse(os.path.exists(ki.TOKEN_FILE))

    def test_open_session_renews_with_keywords_and_saves(self):
        fake = FakeManager()
        fake.token = make_token()
        with open(ki.TOKEN_FILE, "wb") as f:
            pickle.dump(make_token(), f)
        with mock.patch.object(ki, "new_manager", return_value=fake):
            mgr = ki.open_session()
        kw = fake.api.login.call_args.kwargs
        self.assertEqual(kw["username"], "driver@example.com")
        self.assertEqual(kw["password"], "hunter2")
        self.assertEqual(kw["pin"], "1234")
        self.assertEqual(kw["token"].refresh_token, "rm-1")
        self.assertEqual(mgr.token.access_token, "sid-renewed")
        self.assertEqual(mgr.token.pin, "1234")
        self.assertIn("v1", mgr.vehicles)
        self.assertFalse(ki.token_file_changed())

    def test_open_session_without_token_asks_for_otp(self):
        with mock.patch.object(ki, "new_manager", return_value=FakeManager()):
            with self.assertRaises(AuthenticationOTPRequired):
                ki.open_session()

    def test_open_session_dead_rmtoken_asks_for_otp(self):
        fake = FakeManager(renew_result="otp")
        with open(ki.TOKEN_FILE, "wb") as f:
            pickle.dump(make_token(), f)
        with mock.patch.object(ki, "new_manager", return_value=fake):
            with self.assertRaises(AuthenticationOTPRequired):
                ki.open_session()

    def test_is_otp_required_covers_the_wrapper_attribute_error(self):
        self.assertTrue(ki.is_otp_required(AuthenticationOTPRequired("x")))
        self.assertTrue(ki.is_otp_required(
            AttributeError("'OTPRequest' object has no attribute 'access_token'")))
        self.assertFalse(ki.is_otp_required(AttributeError("other")))
        self.assertFalse(ki.is_otp_required(KeyError("payload")))

    def test_recover_session_automatic(self):
        fake = FakeManager()

        def arrive(method):
            FakeIMAP.mailbox[b"1"] = SUBJECT_MAIL
        fake.send_otp = arrive
        with mock.patch.object(ki, "new_manager", return_value=fake), \
                mock.patch.object(ki, "OTP_WAIT_SECONDS", 1), \
                mock.patch.object(ki, "notify") as notify:
            mgr = ki.recover_session("test")
        self.assertIs(mgr, fake)
        self.assertEqual(fake.verified, ["775512"])
        self.assertIn("re-authenticated", notify.call_args.args[0])

    def test_recover_session_manual_waits_for_the_token_file(self):
        with mock.patch.object(ki, "AUTO_OTP", False), \
                mock.patch.object(ki, "REAUTH_COOLDOWN", 0), \
                mock.patch.object(ki, "notify") as notify:
            self.assertIsNone(ki.recover_session("test"))
        self.assertEqual(notify.call_args.kwargs["priority"], "high")

    def test_recover_session_respects_cooldown(self):
        ki._last_reauth = ki.time.monotonic()
        with mock.patch.object(ki, "wait_for_token_file", return_value=False) as wait, \
                mock.patch.object(ki, "auto_otp_login", side_effect=RuntimeError("no")), \
                mock.patch.object(ki, "notify"):
            self.assertIsNone(ki.recover_session("test"))
        # first wait is the cooldown, second the retry wait after the failure
        self.assertEqual(wait.call_count, 2)
        self.assertGreater(wait.call_args_list[0].args[0], 3500)

    def test_token_file_changed_after_external_auth(self):
        fake = FakeManager()
        fake.token = make_token()
        ki.save_token(fake)
        self.assertFalse(ki.token_file_changed())
        os.utime(ki.TOKEN_FILE, (1, 1))          # someone else wrote it
        self.assertTrue(ki.token_file_changed())

    def test_save_token_if_changed_skips_noops(self):
        fake = FakeManager()
        fake.token = make_token()
        ki.save_token(fake)
        first = os.stat(ki.TOKEN_FILE).st_mtime_ns
        ki.save_token_if_changed(fake)
        self.assertEqual(os.stat(ki.TOKEN_FILE).st_mtime_ns, first)
        fake.token = make_token(sid="sid-2")
        ki.save_token_if_changed(fake)
        with open(ki.TOKEN_FILE, "rb") as f:
            self.assertEqual(pickle.load(f).access_token, "sid-2")


if __name__ == "__main__":
    unittest.main()
