"""Structure checks + runs the Code-node logic with Node.js (skipped if Node is missing)."""
import json, shutil, subprocess, unittest
from pathlib import Path
import generate_workflow as gw

HERE = Path(__file__).parent
WF = gw.workflow
NODE = shutil.which("node")


def run_js(code, input_items, refs, static=None):
    """Execute an n8n Code node body in Node with $input, $('Node') and $getWorkflowStaticData stubs."""
    harness = f"""
const refs = {json.dumps(refs)};
const items = {json.dumps(input_items)};
const sd = {json.dumps(static or {})};
const $input = {{ all: () => items, first: () => items[0] }};
const $ = (n) => ({{ all: () => refs[n], first: () => refs[n][0] }});
const $getWorkflowStaticData = () => sd;
const f = async () => {{ {code} }};
f().then(r => console.log(JSON.stringify({{ out: r, sd }})));
"""
    res = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=30)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


class TestStructure(unittest.TestCase):
    def test_file_matches_generator(self):
        on_disk = json.loads((HERE / "Email_Triage_Agent.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, json.loads(json.dumps(WF)))

    def test_connections_point_to_existing_nodes(self):
        names = {n["name"] for n in WF["nodes"]}
        for src, out in WF["connections"].items():
            self.assertIn(src, names)
            for branch in out["main"]:
                for c in branch:
                    self.assertIn(c["node"], names)

    def test_no_secrets_or_private_ids(self):
        text = (HERE / "Email_Triage_Agent.json").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"AIza[0-9A-Za-z_\-]{30,}|\b\d{17,20}\b|\$env\.")

    def test_drafts_by_default(self):
        self.assertIn("autoSend: false", gw.CONFIG_JS)


@unittest.skipUnless(NODE, "Node.js not installed")
class TestLogic(unittest.TestCase):
    cfg = [{"json": {"cfg": {"ownerName": "Kacper", "assistantName": "Baizhi", "autoSend": False, "ignoreSenders": ["no-reply@accounts.google.com"]}}}]

    def test_config_passes_all_items_through(self):
        out = run_js(gw.CONFIG_JS, [{"json": {"id": "1"}}, {"json": {"id": "2"}}], {})["out"]
        self.assertEqual([o["json"]["id"] for o in out], ["1", "2"])
        self.assertFalse(out[0]["json"]["cfg"]["autoSend"])
    mail = [{"json": {"id": "1", "threadId": "t1", "from": {"value": [{"address": "a@b.com", "name": "A"}]}, "subject": "Hi", "text": "Thanks a lot!"}}]

    def norm(self):
        return run_js(gw.NORMALIZE_JS, self.mail, {"Config": self.cfg})["out"]

    def test_normalize(self):
        out = self.norm()[0]["json"]
        self.assertEqual(out["from"], "a@b.com")
        self.assertIn("Return JSON only", out["requestBody"]["contents"][0]["parts"][0]["text"])

    def gemini(self, payload):
        return [{"json": {"candidates": [{"content": {"parts": [{"text": json.dumps(payload) if isinstance(payload, dict) else payload}]}}]}}]

    def route(self, payload):
        return run_js(gw.ROUTE_JS, self.gemini(payload), {"Config": self.cfg, "Normalize & Build Prompt": self.norm()})["out"][0]["json"]

    def test_routine_with_reply(self):
        r = self.route({"category": "routine", "summary": "Thanks", "reply": "Glad it helped! Baizhi", "confidence": 0.9})
        self.assertEqual(r["category"], "routine")
        self.assertFalse(r["autoSend"])

    def test_routine_without_reply_goes_to_human(self):
        self.assertEqual(self.route({"category": "routine", "summary": "x", "reply": ""})["category"], "important")

    def test_bad_model_output_is_safe(self):
        self.assertEqual(self.route("not json")["category"], "important")

    def test_newsletter_queue_and_digest(self):
        q = run_js(gw.QUEUE_NEWS_JS, [{"json": {"fromName": "News", "subject": "S", "summary": "Sum"}}], {})
        self.assertEqual(q["sd"]["newsletters"][0]["subject"], "S")
        d = run_js(gw.DIGEST_JS, [], {}, static=q["sd"])
        self.assertIn("Newsletter digest", d["out"][0]["json"]["content"])
        self.assertEqual(d["sd"]["newsletters"], [])
        self.assertEqual(run_js(gw.DIGEST_JS, [], {}, static={"newsletters": []})["out"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
