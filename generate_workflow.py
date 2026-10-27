"""Builds Email_Triage_Agent.json: Gmail -> Gemini classifier -> 4 routes (urgent / important / routine / newsletter).
Routine mail gets an AI reply saved as a Gmail DRAFT (AUTO_SEND off by default). Newsletters are summarised twice a day."""
import json
from pathlib import Path

HERE = Path(__file__).parent

CONFIG_JS = r"""// Edit these values once after import. Emails pass through unchanged; settings are attached as `cfg`.
const cfg = {
  ownerName: 'Kacper',                       // who the assistant works for
  assistantName: 'Baizhi',                   // signature of AI replies
  autoSend: false,                           // false = routine replies are saved as Gmail drafts (recommended)
  ignoreSenders: ['no-reply@accounts.google.com'], // never auto-answer these
};
return $input.all().map(i => ({ json: { ...i.json, cfg } }));"""

NORMALIZE_JS = r"""// Normalise Gmail output (simple or raw mode) into one shape and build the classifier request.
const cfg = $('Config').first().json.cfg;
const out = [];
for (const it of $input.all()) {
  const m = it.json;
  const fromObj = m.from?.value?.[0] || {};
  const from = fromObj.address || (String(m.From || m.from || '').match(/<([^>]+)>/) || [])[1] || String(m.From || m.from || '');
  const fromName = fromObj.name || String(m.From || '').replace(/<[^>]+>/, '').trim();
  const subject = String(m.subject || m.Subject || '(no subject)');
  const body = String(m.text || m.textPlain || m.snippet || '').replace(/\s+/g, ' ').trim().slice(0, 4000);
  const listUnsub = !!(m.headers && (m.headers['list-unsubscribe'] || m.headers['List-Unsubscribe'])) || /unsubscribe/i.test(body);
  const prompt = `You triage the inbox of ${cfg.ownerName}. Classify the email into exactly one category:
- "urgent": important AND time-critical (client problem, payment issue, security alert, deadline today/tomorrow). ${cfg.ownerName} must handle it personally now.
- "important": matters but is not urgent (proposal, meeting request, partnership, invoice to plan). ${cfg.ownerName} will schedule it.
- "routine": low-stakes personal or business message a polite assistant can answer (thanks, confirmations, simple questions).
- "newsletter": newsletters, promotions, notifications, subscriptions, automated mail.
Return JSON only: {"category": "...", "summary": "max 2 sentences", "reply": "a short friendly reply for routine mail signed '${cfg.assistantName}, assistant to ${cfg.ownerName}', otherwise empty", "confidence": 0.0-1.0}
Never promise dates, prices or commitments in the reply.

From: ${fromName} <${from}>
Subject: ${subject}
Has unsubscribe link: ${listUnsub}
Body:
${body}`;
  out.push({ json: { id: m.id, threadId: m.threadId, from, fromName, subject, body, listUnsub,
    requestBody: { contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0.2, responseMimeType: 'application/json' } } } });
}
return out;"""

ROUTE_JS = r"""// Parse the classifier answer; fall back to safe defaults on bad output.
const cfg = $('Config').first().json.cfg;
const src = $('Normalize & Build Prompt').all();
return $input.all().map((it, i) => {
  const mail = src[i]?.json || {};
  let r = {};
  try { r = JSON.parse(it.json.candidates?.[0]?.content?.parts?.[0]?.text || '{}'); } catch (e) { r = {}; }
  let category = ['urgent', 'important', 'routine', 'newsletter'].includes(r.category) ? r.category : 'important'; // unknown -> a human looks at it
  if (category === 'routine' && (cfg.ignoreSenders || []).some(s => mail.from?.toLowerCase().includes(s))) category = 'newsletter';
  if (category === 'routine' && !String(r.reply || '').trim()) category = 'important';
  return { json: { ...mail, category, summary: String(r.summary || mail.body?.slice(0, 200) || ''), reply: String(r.reply || ''),
    confidence: Number(r.confidence) || 0, autoSend: !!cfg.autoSend, gmailLink: 'https://mail.google.com/mail/u/0/#all/' + (mail.threadId || mail.id || '') } };
});"""

QUEUE_NEWS_JS = r"""// Keep newsletter summaries until the next digest (workflow static data; persists in production runs).
const sd = $getWorkflowStaticData('global');
sd.newsletters = sd.newsletters || [];
for (const it of $input.all()) sd.newsletters.push({ from: it.json.fromName || it.json.from, subject: it.json.subject, summary: it.json.summary, at: new Date().toISOString() });
sd.newsletters = sd.newsletters.slice(-200);
return [{ json: { queued: $input.all().length, total: sd.newsletters.length } }];"""

DIGEST_JS = r"""// Build the twice-daily newsletter digest and clear the queue.
const sd = $getWorkflowStaticData('global');
const items = sd.newsletters || [];
if (!items.length) return [];
sd.newsletters = [];
const lines = items.slice(0, 40).map(n => `• **${n.from}** — ${n.subject}\n  ${n.summary}`);
let content = `📰 **Newsletter digest** (${items.length})\n` + lines.join('\n');
if (content.length > 1950) content = content.slice(0, 1940) + '\n…';
return [{ json: { content } }];"""

DISCORD = {"authentication": "oAuth2", "resource": "message",
           "guildId": {"__rl": True, "value": "YOUR_DISCORD_SERVER_ID", "mode": "id"},
           "channelId": {"__rl": True, "value": "YOUR_DISCORD_CHANNEL_ID", "mode": "id"}, "options": {}}
DCRED = {"discordOAuth2Api": {"id": "", "name": "Discord Bot"}}
GCRED = {"gmailOAuth2": {"id": "", "name": "Gmail"}}


def node(name, typ, ver, pos, params, **extra):
    n = {"parameters": params, "name": name, "type": typ, "typeVersion": ver, "position": pos}
    n.update(extra)
    return n


def discord(name, pos, content):
    return node(name, "n8n-nodes-base.discord", 2, pos, {**DISCORD, "content": content}, credentials=DCRED,
                retryOnFail=True, maxTries=3, waitBetweenTries=5000)


nodes = [
    node("Gmail Trigger", "n8n-nodes-base.gmailTrigger", 1.2, [-900, 0],
         {"pollTimes": {"item": [{"mode": "everyX", "value": 5, "unit": "minutes"}]}, "simple": False,
          "filters": {"labelIds": ["INBOX", "UNREAD"]}, "options": {}}, credentials=GCRED),
    node("Manual Test", "n8n-nodes-base.manualTrigger", 1, [-900, -200], {}),
    node("Sample Emails", "n8n-nodes-base.code", 2, [-680, -200], {"jsCode": r"""return [
 { json: { id: 'demo1', threadId: 'demo1', from: { value: [{ address: 'client@example.com', name: 'Client' }] }, subject: 'Production site is down', text: 'Our checkout returns 500 since 9:00. Can you look at it today?' } },
 { json: { id: 'demo2', threadId: 'demo2', from: { value: [{ address: 'partner@example.com', name: 'Partner' }] }, subject: 'Partnership idea for Q1', text: 'Would you be open to a call next week about a joint offer?' } },
 { json: { id: 'demo3', threadId: 'demo3', from: { value: [{ address: 'friend@example.com', name: 'Friend' }] }, subject: 'Thanks!', text: 'Thanks for the tips yesterday, very helpful.' } },
 { json: { id: 'demo4', threadId: 'demo4', from: { value: [{ address: 'news@example.com', name: 'Weekly Dev News' }] }, subject: 'This week in AI', text: 'Top stories... unsubscribe here.' } },
];"""}),
    node("Config", "n8n-nodes-base.code", 2, [-460, 0], {"jsCode": CONFIG_JS}),
    node("Normalize & Build Prompt", "n8n-nodes-base.code", 2, [-240, 0], {"jsCode": NORMALIZE_JS}),
    node("Gemini Classifier", "n8n-nodes-base.httpRequest", 4.2, [-20, 0], {
        "method": "POST", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "googlePalmApi",
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.requestBody) }}", "options": {"timeout": 60000}},
        credentials={"googlePalmApi": {"id": "", "name": "Gemini API"}}, retryOnFail=True, maxTries=3, waitBetweenTries=5000),
    node("Parse & Route", "n8n-nodes-base.code", 2, [200, 0], {"jsCode": ROUTE_JS}),
    node("Category", "n8n-nodes-base.switch", 3.2, [420, 0], {"rules": {"values": [
        {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2}, "combinator": "and",
                        "conditions": [{"leftValue": "={{ $json.category }}", "rightValue": c, "operator": {"type": "string", "operation": "equals"}}]},
         "renameOutput": True, "outputKey": c} for c in ["urgent", "important", "routine", "newsletter"]]}, "options": {}}),
    discord("Discord: Urgent", [700, -300],
            "=🔴 **Urgent email** — handle personally\n**From:** {{ $json.fromName }} <{{ $json.from }}>\n**Subject:** {{ $json.subject }}\n{{ $json.summary }}\n<{{ $json.gmailLink }}>"),
    discord("Discord: Plan It", [700, -120],
            "=🟡 **Important, not urgent** — schedule it\n**From:** {{ $json.fromName }} <{{ $json.from }}>\n**Subject:** {{ $json.subject }}\n{{ $json.summary }}\n<{{ $json.gmailLink }}>"),
    node("Auto Send?", "n8n-nodes-base.if", 2.2, [700, 60], {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
        "conditions": [{"leftValue": "={{ $json.autoSend }}", "rightValue": True, "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}}),
    node("Gmail: Send Reply", "n8n-nodes-base.gmail", 2.1, [940, -20], {"resource": "message", "operation": "reply",
        "messageId": "={{ $json.id }}", "emailType": "text", "message": "={{ $json.reply }}", "options": {}}, credentials=GCRED),
    node("Gmail: Save Draft", "n8n-nodes-base.gmail", 2.1, [940, 140], {"resource": "draft", "operation": "create",
        "subject": "=Re: {{ $json.subject }}", "emailType": "text", "message": "={{ $json.reply }}",
        "options": {"threadId": "={{ $json.threadId }}", "sendTo": "={{ $json.from }}"}}, credentials=GCRED),
    discord("Discord: Reply Note", [1180, 60],
            "=🟢 **Routine email answered** ({{ $('Parse & Route').item.json.autoSend ? 'sent' : 'draft saved, review in Gmail' }})\n**To:** {{ $('Parse & Route').item.json.from }} — {{ $('Parse & Route').item.json.subject }}\n**Reply:** {{ $('Parse & Route').item.json.reply.slice(0, 600) }}"),
    node("Queue Newsletter", "n8n-nodes-base.code", 2, [700, 260], {"jsCode": QUEUE_NEWS_JS}),
    node("Twice a Day (07:30, 19:30)", "n8n-nodes-base.scheduleTrigger", 1.2, [420, 460],
         {"rule": {"interval": [{"field": "cronExpression", "expression": "30 7,19 * * *"}]}}),
    node("Build Newsletter Digest", "n8n-nodes-base.code", 2, [700, 460], {"jsCode": DIGEST_JS}),
    discord("Discord: Newsletter Digest", [940, 460], "={{ $json.content }}"),
]
C = lambda *targets: {"main": [[{"node": t, "type": "main", "index": 0} for t in targets]]}
conns = {
    "Gmail Trigger": C("Config"), "Manual Test": C("Sample Emails"), "Sample Emails": C("Config"),
    "Config": C("Normalize & Build Prompt"), "Normalize & Build Prompt": C("Gemini Classifier"),
    "Gemini Classifier": C("Parse & Route"), "Parse & Route": C("Category"),
    "Category": {"main": [[{"node": "Discord: Urgent", "type": "main", "index": 0}], [{"node": "Discord: Plan It", "type": "main", "index": 0}],
                          [{"node": "Auto Send?", "type": "main", "index": 0}], [{"node": "Queue Newsletter", "type": "main", "index": 0}]]},
    "Auto Send?": {"main": [[{"node": "Gmail: Send Reply", "type": "main", "index": 0}], [{"node": "Gmail: Save Draft", "type": "main", "index": 0}]]},
    "Gmail: Send Reply": C("Discord: Reply Note"), "Gmail: Save Draft": C("Discord: Reply Note"),
    "Twice a Day (07:30, 19:30)": C("Build Newsletter Digest"), "Build Newsletter Digest": C("Discord: Newsletter Digest"),
}
workflow = {"name": "Email Triage Agent", "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1"}}

if __name__ == "__main__":
    out = HERE / "Email_Triage_Agent.json"
    out.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}")
