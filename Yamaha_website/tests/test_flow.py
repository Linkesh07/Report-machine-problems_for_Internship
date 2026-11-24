"""End-to-end check of the portal: logins, access rules, duplicate detection, manager tools.

Run from the project folder:   python -m unittest tests.test_flow -v
Uses a temporary database, so your real data is never touched.
"""
import io
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault("USE_EMBEDDINGS", "0")  # fast keyword matching for tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db  # noqa: E402
from app.models import Issue, Machine, Report, User  # noqa: E402


class PortalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{cls.tmp.name}/test.db",
            "UPLOAD_FOLDER": cls.tmp.name,
        })
        with cls.app.app_context():
            m = User(employee_id="MGR001", name="Factory Manager", role="manager"); m.set_password("Manager@123")
            db.session.add(m)
            for eid, name in [("WRK001", "Ravi"), ("WRK002", "Kumar"), ("WRK003", "Anu")]:
                u = User(employee_id=eid, name=name, role="worker"); u.set_password("Worker@123")
                db.session.add(u)
            for code, name, dept in [("WELD-01", "Welding Robot", "Welding"), ("PAINT-01", "Paint Booth", "Painting"), ("ENG-01", "Engine Assembly Line", "Assembly"), ("OVEN-01", "Curing Oven", "Painting")]:
                db.session.add(Machine(machine_code=code, name=name, department=dept))
            db.session.commit()
            cls.weld = Machine.query.filter_by(machine_code="WELD-01").first().id
            cls.paint = Machine.query.filter_by(machine_code="PAINT-01").first().id
            cls.eng = Machine.query.filter_by(machine_code="ENG-01").first().id
            cls.oven = Machine.query.filter_by(machine_code="OVEN-01").first().id

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def client(self):
        return self.app.test_client()

    def login(self, c, role, eid, pw):
        return c.post(f"/{role}-login", data={"employee_id": eid, "password": pw}, follow_redirects=False)

    def report(self, c, machine, title, message, severity="High"):
        return c.post("/worker/report/new", data={
            "machine_id": machine, "title": title, "message": message, "severity": severity,
        }, follow_redirects=False)

    # ------------------------------------------------------------ the tests run in this order
    def test_1_public_pages_and_two_logins(self):
        c = self.client()
        for url in ("/", "/worker-login", "/manager-login"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get("/nope").status_code, 404)

    def test_2_wrong_door_is_refused(self):
        c = self.client()
        r = self.login(c, "manager", "WRK001", "Worker@123")   # worker on manager page
        self.assertEqual(r.status_code, 200)
        r = self.login(c, "worker", "MGR001", "Manager@123")   # manager on worker page
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/worker/", follow_redirects=False).status_code, 302)  # still logged out

    def test_3_protected_pages_need_the_right_role(self):
        anon = self.client()
        self.assertEqual(anon.get("/manager/").status_code, 302)
        self.assertEqual(anon.get("/worker/").status_code, 302)
        w = self.client(); self.assertEqual(self.login(w, "worker", "WRK001", "Worker@123").status_code, 302)
        self.assertEqual(w.get("/manager/").status_code, 403)       # Access Denied
        self.assertEqual(w.get("/manager/workers").status_code, 403)
        m = self.client(); self.assertEqual(self.login(m, "manager", "MGR001", "Manager@123").status_code, 302)
        self.assertEqual(m.get("/worker/").status_code, 403)

    def test_4_duplicate_scenario(self):
        ravi, kumar, anu = self.client(), self.client(), self.client()
        self.login(ravi, "worker", "WRK001", "Worker@123")
        self.login(kumar, "worker", "WRK002", "Worker@123")
        self.login(anu, "worker", "WRK003", "Worker@123")

        self.assertEqual(self.report(ravi, self.weld, "Welding robot stops suddenly",
                                     "Welding robot stops suddenly and shows red light.").status_code, 302)
        self.assertEqual(self.report(kumar, self.weld, "Welding machine shuts down",
                                     "The welding machine is shutting down by itself with a red warning light.").status_code, 302)
        self.assertEqual(self.report(anu, self.paint, "Paint booth fan making loud noise",
                                     "Paint booth fan making loud noise since morning.", "Medium").status_code, 302)

        with self.app.app_context():
            first, second, third = Report.query.order_by(Report.id).all()
            print(f"\n   Ravi  -> {first.match_status}")
            print(f"   Kumar -> {second.match_status} ({second.similarity_score:.0%} similar to #{second.duplicate_of_id})")
            print(f"   Anu   -> {third.match_status}")
            self.assertEqual(first.match_status, "new")
            self.assertIn(second.match_status, ("duplicate", "possible"))
            self.assertEqual(third.match_status, "new")            # different machine
            if second.match_status == "duplicate":
                self.assertEqual(second.issue_id, first.issue_id)
                self.assertEqual(first.issue.reporter_count, 2)    # "reported by 2 workers"

    def test_5_workers_see_only_their_own_reports(self):
        kumar = self.client(); self.login(kumar, "worker", "WRK002", "Worker@123")
        with self.app.app_context():
            ravis = Report.query.filter_by(worker_id=User.query.filter_by(employee_id="WRK001").first().id).first().id
        self.assertEqual(kumar.get(f"/worker/reports/{ravis}").status_code, 404)

    def test_6_every_manager_page_opens(self):
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        with self.app.app_context():
            rid = Report.query.order_by(Report.id.desc()).first().id
            dup = Report.query.filter(Report.match_status != "new").first()
            iid = Issue.query.first().id
        urls = ["/manager/", "/manager/reports", "/manager/reports?match=duplicate&q=weld&severity=High",
                f"/manager/reports/{rid}", f"/manager/issues", f"/manager/issues/{iid}",
                "/manager/workers", "/manager/workers/new", "/manager/api/notifications", "/account/password"]
        if dup:
            urls.append(f"/manager/reports/{dup.id}")
        for url in urls:
            self.assertEqual(m.get(url).status_code, 200, url)
        body = m.get(f"/manager/reports/{dup.id}").get_data(as_text=True) if dup else ""
        if dup:
            self.assertIn("similar", body)

    def test_7_every_worker_page_opens(self):
        w = self.client(); self.login(w, "worker", "WRK001", "Worker@123")
        with self.app.app_context():
            rid = Report.query.filter_by(worker_id=User.query.filter_by(employee_id="WRK001").first().id).first().id
        for url in ("/worker/", "/worker/report/new", "/worker/reports", "/worker/reports?status=Pending", f"/worker/reports/{rid}"):
            self.assertEqual(w.get(url).status_code, 200, url)

    def test_8_manager_actions(self):
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        with self.app.app_context():
            issue = Issue.query.first(); iid = issue.id
        r = m.post(f"/manager/issues/{iid}/note", data={"note_text": "Technician called."}, follow_redirects=True)
        self.assertIn("Technician called.", r.get_data(as_text=True))
        m.post(f"/manager/issues/{iid}/status", data={"status": "In Progress"})
        m.post(f"/manager/issues/{iid}/priority", data={"priority": "Critical"})
        with self.app.app_context():
            issue = db.session.get(Issue, iid)
            self.assertEqual(issue.status, "In Progress")
            self.assertEqual(issue.priority, "Critical")
            self.assertTrue(all(x.status == "In Progress" for x in issue.reports))
        # worker management
        m.post("/manager/workers/new", data={"employee_id": "wrk009", "name": "Meena", "password": "Temp@1234"})
        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(employee_id="WRK009").first())
        m.post("/manager/workers/new", data={"employee_id": "WRK010", "name": "Weak", "password": "abc"})
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(employee_id="WRK010").first())  # weak password refused

    def test_9_photo_upload_rules(self):
        w = self.client(); self.login(w, "worker", "WRK001", "Worker@123")
        bad = w.post("/worker/report/new", data={
            "machine_id": self.paint, "title": "Fan wobble", "message": "The fan wobbles at high speed badly.",
            "severity": "Low", "photo": (io.BytesIO(b"not an image"), "x.exe")}, content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 200)  # form shown again with an error
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 40), "red").save(buf, "PNG"); buf.seek(0)
        ok = w.post("/worker/report/new", data={
            "machine_id": self.paint, "title": "Belt slipping", "message": "The conveyor belt is slipping at the end.",
            "severity": "Low", "photo": (buf, "belt.png")}, content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 302)

    # ------------------------------------------------------------ duplicate accuracy + deleting
    LONG_OVERHEAT = (
        "Engine overheating occurs when the engine temperature becomes too high. It may happen due to low engine "
        "coolant or insufficient engine oil. A damaged radiator or cooling fan can also cause overheating. It can "
        "reduce engine performance and damage engine parts. To prevent this problem, check coolant, engine oil, "
        "radiator, and cooling fan regularly.")

    def test_A_short_and_long_overheating_reports_are_duplicates(self):
        ravi, kumar, anu = self.client(), self.client(), self.client()
        self.login(ravi, "worker", "WRK001", "Worker@123")
        self.login(kumar, "worker", "WRK002", "Worker@123")
        self.login(anu, "worker", "WRK003", "Worker@123")
        self.report(ravi, self.eng, "Engine Overheating Problem", self.LONG_OVERHEAT)
        self.report(kumar, self.eng, "Engine overheating", "engine overheating in the machine")
        self.report(anu, self.eng, "Engine oil leak", "Engine oil is dripping under the engine onto the floor.")
        with self.app.app_context():
            first, second, third = Report.query.filter_by(machine_id=self.eng).order_by(Report.id).all()
            print(f"\n   long vs short overheating -> {second.match_status} ({second.similarity_score:.0%})")
            print(f"   overheating vs oil leak   -> {third.match_status}")
            self.assertEqual(first.match_status, "new")
            self.assertEqual(second.match_status, "duplicate")
            self.assertEqual(second.issue_id, first.issue_id)
            self.assertEqual(first.issue.reporter_count, 2)
            self.assertEqual(third.match_status, "new")  # a different kind of problem stays separate

    def test_B_manager_can_delete_checked_reports(self):
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        with self.app.app_context():
            r1, r2, _ = Report.query.filter_by(machine_id=self.eng).order_by(Report.id).all()
            id1, id2, iid = r1.id, r2.id, r1.issue_id

        # not checked yet -> refused
        m.post(f"/manager/reports/{id2}/delete")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Report, id2))

        # list shows a Delete button only for checked reports
        listing = m.get("/manager/reports?q=Engine").get_data(as_text=True)
        self.assertNotIn(f"/manager/reports/{id2}/delete", listing)
        m.get(f"/manager/reports/{id1}")  # opening a report marks it as checked
        listing = m.get("/manager/reports?q=Engine").get_data(as_text=True)
        self.assertIn(f"/manager/reports/{id1}/delete", listing)

        # delete the MAIN report: the other one must take over as main report of the issue
        m.post(f"/manager/reports/{id1}/delete")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Report, id1))
            issue = db.session.get(Issue, iid)
            self.assertIsNotNone(issue)
            self.assertEqual(issue.main_report_id, id2)
            self.assertEqual(db.session.get(Report, id2).match_status, "new")
            self.assertIsNone(db.session.get(Report, id2).duplicate_of_id)

        # delete the last report of the issue: the issue goes too
        m.get(f"/manager/reports/{id2}")
        m.post(f"/manager/reports/{id2}/delete")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Report, id2))
            self.assertIsNone(db.session.get(Issue, iid))

    def test_C_manager_can_delete_a_worker_with_reports(self):
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        m.post("/manager/workers/new", data={"employee_id": "WRK020", "name": "Selvi", "password": "Temp@1234"})
        w = self.client(); self.assertEqual(self.login(w, "worker", "WRK020", "Temp@1234").status_code, 302)
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (30, 30), "blue").save(buf, "PNG"); buf.seek(0)
        w.post("/worker/report/new", data={
            "machine_id": self.paint, "title": "Spray gun clogged", "message": "The spray gun nozzle is clogged badly.",
            "severity": "Low", "photo": (buf, "gun.png")}, content_type="multipart/form-data")
        with self.app.app_context():
            worker = User.query.filter_by(employee_id="WRK020").first()
            uid = worker.id
            report = Report.query.filter_by(worker_id=uid).first()
            photo = os.path.join(self.tmp.name, report.photo_path)
            self.assertTrue(os.path.exists(photo))
        r = m.post(f"/manager/workers/{uid}/delete", follow_redirects=True)
        self.assertIn("was deleted", r.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(db.session.get(User, uid))
            self.assertEqual(Report.query.filter_by(worker_id=uid).count(), 0)
            self.assertFalse(os.path.exists(photo))  # the photo file is removed too

    def test_D_delete_warns_when_the_issue_is_not_solved(self):
        w = self.client(); self.login(w, "worker", "WRK001", "Worker@123")
        self.report(w, self.paint, "Spray hose cracked", "The spray hose is cracked near the handle.", "Low")
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        with self.app.app_context():
            rep = Report.query.filter_by(title="Spray hose cracked").first()
            rid, iid = rep.id, rep.issue_id
        m.get(f"/manager/reports/{rid}")  # check the report
        page = m.get("/manager/reports?q=Spray+hose").get_data(as_text=True)
        self.assertIn(f"Report #{rid} is NOT solved yet (status: Pending)", page)
        self.assertIn("Not solved yet.", m.get(f"/manager/reports/{rid}").get_data(as_text=True))
        m.post(f"/manager/issues/{iid}/status", data={"status": "Resolved"})
        page = m.get("/manager/reports?q=Spray+hose").get_data(as_text=True)
        self.assertNotIn("NOT solved yet", page)  # solved -> normal confirm text only

    def test_E_different_words_for_the_same_problem(self):
        ravi, kumar, anu = self.client(), self.client(), self.client()
        self.login(ravi, "worker", "WRK001", "Worker@123")
        self.login(kumar, "worker", "WRK002", "Worker@123")
        self.login(anu, "worker", "WRK003", "Worker@123")
        self.report(ravi, self.oven, "Temperature reducing rapidly", "The temperature is reducing rapidly in the machine.")
        self.report(kumar, self.oven, "Heat decreased suddenly", "The heat is suddenly get decreased in the machine.")
        self.report(anu, self.oven, "Temperature rising rapidly", "The temperature is rising rapidly in the machine.")
        with self.app.app_context():
            first, second, third = Report.query.filter_by(machine_id=self.oven).order_by(Report.id).all()
            print(f"\n   temperature reducing vs heat decreased -> {second.match_status} ({second.similarity_score:.0%})")
            print(f"   temperature rising (opposite)          -> {third.match_status}")
            self.assertEqual(second.match_status, "duplicate")   # different words, same problem
            self.assertEqual(second.issue_id, first.issue_id)
            self.assertEqual(third.match_status, "new")          # the opposite direction is not a duplicate

    def test_F_your_own_words_in_synonyms_json(self):
        import importlib, json
        from app import duplicate_detection as dd
        path = os.path.join(self.tmp.name, "my_synonyms.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"_help": "note", "leak": "weep", "coolant": "antifreeze", "_symptoms": ["coolant"]}, fh)
        os.environ["SYNONYMS_FILE"] = path
        try:
            importlib.reload(dd)
            self.assertEqual(dd.normalize("pipe is weeping"), dd.normalize("pipe is leaking"))
            self.assertIn("coolant", dd.SYMPTOMS)
        finally:
            os.environ.pop("SYNONYMS_FILE", None)
            importlib.reload(dd)  # back to the normal word list for the other tests
        self.assertNotEqual(dd.normalize("pipe is weeping"), dd.normalize("pipe is leaking"))

    def test_G_word_groups_do_not_clash(self):
        from app.duplicate_detection import GROUPS, STOP_WORDS
        seen = {}
        for name, words in GROUPS.items():
            for word in words.split():
                self.assertNotIn(word, seen, f"'{word}' is in both '{seen.get(word)}' and '{name}'")
                self.assertNotIn(word, STOP_WORDS, f"'{word}' is a group word but also ignored")
                seen[word] = name

    def test_H_word_forms_and_tenses(self):
        from app.duplicate_detection import classify, normalize, similarity_scores
        for word in ("noise", "noises", "noisy", "noisier", "noisily", "loud"):
            self.assertEqual(normalize(word), ["noise"], word)
        for word in ("noiseless", "noiselessly"):  # "no noise" is the opposite of a noise complaint
            self.assertEqual(normalize(word), ["quiet"], word)
        for word in ("stop", "stopped", "stopping", "stops"):
            self.assertEqual(normalize(word), ["stop"], word)
        for word in ("shook", "shaking", "vibration", "vibrated"):
            self.assertEqual(normalize(word), ["vibrate"], word)

        def label(a, b):
            return classify(similarity_scores(a, [b])[0])

        self.assertEqual(label("the is more noisy and polluting the environment.",
                               "due the over work the machine prouding noise."), "duplicate")
        self.assertEqual(label("The pump stopped", "the pump is stopping again"), "duplicate")
        self.assertEqual(label("machine is noiseless now", "machine makes noise"), "new")
        self.assertEqual(label("fan is noisy", "gearbox noise"), "new")

    def test_I_worker_ids_in_order_and_workers_read_manager_notes(self):
        m = self.client(); self.login(m, "manager", "MGR001", "Manager@123")
        for eid, name in (("WRK0011", "Late"), ("WRK0010", "Ten")):
            m.post("/manager/workers/new", data={"employee_id": eid, "name": name, "password": "Temp@1234"})
        page = m.get("/manager/workers").get_data(as_text=True)
        order = [page.index(f"<strong>{eid}</strong>") for eid in ("WRK001", "WRK002", "WRK003", "WRK009", "WRK0010", "WRK0011")]
        self.assertEqual(order, sorted(order))              # 1, 2, 3, 9, 10, 11 - not 1, 10, 11, 2
        self.assertIn('value="WRK012"', m.get("/manager/workers/new").get_data(as_text=True))

        # a note written by the manager is shown to the workers who reported that issue
        with self.app.app_context():
            ravi_id = User.query.filter_by(employee_id="WRK001").first().id
            report = Report.query.filter_by(worker_id=ravi_id).order_by(Report.id).first()
            rid, iid = report.id, report.issue_id
            kumar_report = Report.query.filter(Report.issue_id == iid, Report.worker_id != ravi_id).first()
        m.post(f"/manager/issues/{iid}/note", data={"note_text": "Please keep the area clear. Technician at 3 pm."})
        ravi = self.client(); self.login(ravi, "worker", "WRK001", "Worker@123")
        self.assertIn("Technician at 3 pm.", ravi.get(f"/worker/reports/{rid}").get_data(as_text=True))
        self.assertIn("Manager message", ravi.get("/worker/reports").get_data(as_text=True))
        if kumar_report:                                     # the duplicate reporter reads it too
            kumar = self.client(); self.login(kumar, "worker", "WRK002", "Worker@123")
            self.assertIn("Technician at 3 pm.", kumar.get(f"/worker/reports/{kumar_report.id}").get_data(as_text=True))
        anu = self.client(); self.login(anu, "worker", "WRK003", "Worker@123")   # other issue: not shown
        self.assertNotIn("Technician at 3 pm.", anu.get("/worker/reports").get_data(as_text=True))

    def test_Z_lockout_after_five_wrong_passwords(self):
        c = self.client()
        for _ in range(5):
            self.login(c, "worker", "WRK003", "wrong")
        r = self.login(c, "worker", "WRK003", "Worker@123")  # right password, but locked
        self.assertEqual(r.status_code, 200)
        self.assertIn("locked", r.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
