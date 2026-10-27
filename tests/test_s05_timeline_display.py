"""Timeline labels distinguish plugin provenance from game scenes."""
import shutil
import subprocess
import unittest
from pathlib import Path


class TimelineDisplayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the existing UI script")
    def test_plugin_chats_and_explicit_game_scenes_have_distinct_labels(self):
        script = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const nodes = new Map([
  ['[data-page="timeline"] .tl', {innerHTML: ""}],
  ['[data-page="timeline"] .toolbar .chips', {innerHTML: ""}],
  ["#dtTag", {}], ["#dtSrc", {}], ["#dtType", {}],
]);
const window = {__RIN_SERVICE__: true};
const context = {window, document: {querySelector: key => nodes.get(key) || null,
  querySelectorAll: () => [], addEventListener: () => {}},
  location: {origin: "http://test"}, sessionStorage: {getItem: () => null},
  fetch: () => new Promise(() => {}), setInterval: () => 0};
const source = fs.readFileSync(process.argv[1], "utf8").replace(/\}\)\(\);\s*$/,
  "window.testViews = {renderTimeline, selectLiveEvent, sourceLabel};})();");
vm.runInNewContext(source, context);
const {renderTimeline, selectLiveEvent, sourceLabel} = window.testViews;
const events = [
  {id: "dsh", scene: "plugin:dsh_memory", source_key: "plugin:dsh_memory:companion:private:qq:1"},
  {id: "plugin", scene: "plugin:game_helper", source_key: "plugin:game_helper:companion:group:qq:2"},
  {id: "host", scene: "host:game_chat", source_key: "host:companion:private:3"},
  {id: "game", scene: "game:sts2", source_key: "plugin:sts2_autoplay:companion:private:run:4"},
].map(event => ({...event, text: event.id, kind: "user_message", content_class: "chat",
  occurred_at: "2026-10-26T08:00:00+08:00"}));
window.RIN_LIVE.snapshot = {timeline: {events}};
renderTimeline();
const html = nodes.get('[data-page="timeline"] .tl').innerHTML;
const chips = nodes.get('[data-page="timeline"] .toolbar .chips').innerHTML;
assert.match(html, /data-t="对话"[^>]*data-src="DSH 对话"[^>]*data-id="dsh"/);
assert.match(html, /data-t="对话"[^>]*data-src="插件会话 · game_helper"[^>]*data-id="plugin"/);
assert.match(html, /data-t="对话"[^>]*data-id="host"/);
assert.match(html, /data-t="游戏"[^>]*data-src="游戏会话"[^>]*data-id="game"/);
assert.match(chips, /data-filter="对话">对话 <span class="n">3<\/span>/);
assert.match(chips, /data-filter="游戏">游戏 <span class="n">1<\/span>/);
selectLiveEvent("dsh");
assert.equal(nodes.get("#dtSrc").textContent, "DSH 对话");
assert.equal(nodes.get("#dtType").textContent, "对话");
assert.equal(nodes.get("#dtTag").className, "tag t-talk");
selectLiveEvent("game");
assert.equal(nodes.get("#dtSrc").textContent, "游戏会话");
assert.equal(nodes.get("#dtType").textContent, "游戏");
assert.equal(sourceLabel([events[1].source_key]), "插件");
'''
        source = Path(__file__).resolve().parents[1] / "webui/designs/tidebell/live.js"
        result = subprocess.run([shutil.which("node"), "-e", script, str(source)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
