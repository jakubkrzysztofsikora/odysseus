"""Group chat front-end sequencing smoke tests.

The group module is browser-heavy, so this follows the lightweight Node
approach used by other JS tests in this repo: provide small DOM/fetch stubs and
exercise the real ES module without adding a full browser test framework.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture(scope="module")
def node_available():
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")


def _run_node(script: str) -> dict:
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=_REPO,
        capture_output=True,
        timeout=20,
        text=True,
    )
    if res.returncode != 0:
        raise AssertionError(f"node failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    out_lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not out_lines:
        raise AssertionError("node produced no stdout")
    return json.loads(out_lines[-1])


def test_sequential_group_uses_previous_agent_output_as_next_input(node_available):
    script = textwrap.dedent(
        """
        class El {
          constructor(tag = 'div') {
            this.tagName = tag.toUpperCase();
            this.children = [];
            this.style = { setProperty(){}, removeProperty(){} };
            this.dataset = {};
            this.className = '';
            this.id = '';
            this.type = '';
            this.value = '';
            this.textContent = '';
            this._innerHTML = '';
            this.parentElement = null;
            this.classList = { contains: () => false, add(){}, remove(){}, toggle(){} };
          }
          appendChild(el) { this.children.push(el); el.parentElement = this; return el; }
          prepend(el) { this.children.unshift(el); el.parentElement = this; return el; }
          remove() {}
          addEventListener() {}
          removeEventListener() {}
          dispatchEvent() { return true; }
          closest() { return null; }
          querySelector(sel) {
            if (sel === '.role') return this._role || this.children.find(c => c.className === 'role') || null;
            if (sel === '.body') return this._body || this.children.find(c => c.className === 'body') || null;
            return null;
          }
          querySelectorAll() { return []; }
          set innerHTML(v) {
            this._innerHTML = String(v);
            if (this._innerHTML.includes('class="role"') && this._innerHTML.includes('class="body"')) {
              this._role = new El('div');
              this._role.className = 'role';
              this._body = new El('div');
              this._body.className = 'body';
              this.children = [this._role, this._body];
            } else if (v === '') {
              this.children = [];
            }
          }
          get innerHTML() { return this._innerHTML; }
          setAttribute() {}
          removeAttribute() {}
        }
        class HTMLInputElement extends El {}
        class HTMLTextAreaElement extends El {}
        class HTMLSelectElement extends El {}
        const chatHistory = new El('div');

        globalThis.Element = El;
        globalThis.HTMLElement = El;
        globalThis.HTMLInputElement = HTMLInputElement;
        globalThis.HTMLTextAreaElement = HTMLTextAreaElement;
        globalThis.HTMLSelectElement = HTMLSelectElement;
        globalThis.MutationObserver = class { observe(){} disconnect(){} };
        globalThis.ResizeObserver = class { observe(){} disconnect(){} };
        globalThis.Event = class { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } };
        globalThis.CustomEvent = globalThis.Event;
        globalThis.window = {
          location: { origin: 'http://test' },
          addEventListener(){},
          removeEventListener(){},
          matchMedia: () => ({ matches: false, addEventListener(){}, removeEventListener(){} }),
          HTMLInputElement,
          Event: globalThis.Event,
          CustomEvent: globalThis.CustomEvent,
        };
        globalThis.document = {
          contains: () => true,
          createElement: (tag) => tag === 'input' ? new HTMLInputElement(tag) : new El(tag),
          getElementById: (id) => id === 'chat-history' ? chatHistory : null,
          addEventListener(){},
          removeEventListener(){},
          querySelector: () => null,
          querySelectorAll: () => [],
          body: new El('body'),
          documentElement: new El('html'),
        };
        globalThis.localStorage = {
          getItem: (key) => key === 'odysseus-workspace' ? '/Users/jakubsikora/Repos/personal/odysseus' : null,
          setItem(){},
          removeItem(){},
        };
        globalThis.history = { replaceState(){} };
        globalThis.performance = { now: () => Date.now() };
        globalThis.requestAnimationFrame = () => 0;
        globalThis.cancelAnimationFrame = () => {};
        globalThis.setInterval = () => 0;
        globalThis.clearInterval = () => {};
        Object.defineProperty(globalThis, 'navigator', {
          value: { clipboard: { writeText: async () => {} } },
          configurable: true,
        });

        const streamInputs = [];
        const injectedSystemPrompts = [];
        let sessionSeq = 0;
        globalThis.fetch = async (url, opts = {}) => {
          const path = String(url);
          if (path.includes('/api/tts/stats') || path.includes('/api/prefs/custom-themes')) {
            return new Response(JSON.stringify({ ok: true }), { status: 200 });
          }
          if (path.endsWith('/api/session')) {
            return new Response(JSON.stringify({ id: `sess-${sessionSeq++}` }), { status: 200 });
          }
          if (path.includes('/inject_messages')) {
            const payload = JSON.parse(opts.body || '{}');
            const sys = (payload.messages || []).find(m => m.role === 'system');
            if (sys) injectedSystemPrompts.push(sys.content);
            return new Response(JSON.stringify({ ok: true }), { status: 200 });
          }
          if (path.endsWith('/api/chat_stream')) {
            const message = opts.body.get('message');
            const session = opts.body.get('session');
            streamInputs.push({
              session,
              message,
              mode: opts.body.get('mode'),
              multiagent: opts.body.get('multiagent'),
              allowBash: opts.body.get('allow_bash'),
              allowWebSearch: opts.body.get('allow_web_search'),
              workspace: opts.body.get('workspace'),
            });
            const text = streamInputs.length === 1
              ? 'mcp__0ac61a6b__search\\\\Eloquent{"query":"Circit internal AI"}\\nagent-1-output'
              : `agent-${streamInputs.length}-output`;
            return new Response(`data: ${JSON.stringify({ delta: text })}\\n\\ndata: [DONE]\\n\\n`, {
              status: 200,
              headers: { 'Content-Type': 'text/event-stream' },
            });
          }
          return new Response(JSON.stringify({ ok: true }), { status: 200 });
        };

        const group = await import('./static/js/group.js');
        group.init('');
        group.setMode('round-robin');
        await group.startGroup([
          { mid: 'mistral', display: 'Alpha', url: 'http://llm/v1' },
          { mid: 'kimi', display: 'Beta', url: 'http://llm/v1' },
          { mid: 'glm-5.1', display: 'Gamma', url: 'http://llm/v1' },
        ], null);
        await group.sendMessage('initial prompt');

        console.log(JSON.stringify({
          streamInputs,
          injectedSystemPrompts,
          firstBodyHtml: chatHistory.children[0]._body._innerHTML,
        }));
        process.exit(0);
        """
    )

    out = _run_node(script)
    assert out["streamInputs"][0] == {
        "session": "sess-1",
        "message": "initial prompt",
        "mode": "agent",
        "multiagent": "true",
        "allowBash": "true",
        "allowWebSearch": "true",
        "workspace": "/Users/jakubsikora/Repos/personal/odysseus",
    }
    assert out["streamInputs"][1]["session"] == "sess-2"
    assert out["streamInputs"][1]["mode"] == "agent"
    assert out["streamInputs"][1]["multiagent"] == "true"
    assert out["streamInputs"][1]["allowBash"] == "true"
    assert out["streamInputs"][1]["allowWebSearch"] == "true"
    assert out["streamInputs"][1]["workspace"] == "/Users/jakubsikora/Repos/personal/odysseus"
    assert "Sequential group handoff." in out["streamInputs"][1]["message"]
    assert "Previous participant (Alpha) output:" in out["streamInputs"][1]["message"]
    assert "agent-1-output" in out["streamInputs"][1]["message"]
    assert "Original user task for context only:" in out["streamInputs"][1]["message"]
    assert "initial prompt" in out["streamInputs"][1]["message"]

    assert out["streamInputs"][2]["session"] == "sess-3"
    assert out["streamInputs"][2]["mode"] == "agent"
    assert out["streamInputs"][2]["multiagent"] == "true"
    assert "Previous participant (Beta) output:" in out["streamInputs"][2]["message"]
    assert "agent-2-output" in out["streamInputs"][2]["message"]
    assert "Original user task for context only:" in out["streamInputs"][2]["message"]
    assert "initial prompt" in out["streamInputs"][2]["message"]
    assert out["injectedSystemPrompts"]
    assert all(
        "trusted context from this current group run" in prompt
        for prompt in out["injectedSystemPrompts"]
    )
    assert all(
        "authorized internal planning workflow" in prompt
        and "redact secrets" in prompt
        and "concrete artifact for your role" in prompt
        for prompt in out["injectedSystemPrompts"]
    )
    assert "mcp__0ac61a6b__search" not in out["firstBodyHtml"]
    assert "agent-1-output" in out["firstBodyHtml"]
