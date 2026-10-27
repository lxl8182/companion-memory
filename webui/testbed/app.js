/* RIN C04 testbed: vanilla same-origin API client. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { snapshot: null, engine: null, search: null, busy: false };
  const READONLY = () => Boolean(state.engine && state.engine.read_only);

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function rid(prefix) {
    const suffix = (globalThis.crypto && crypto.randomUUID)
      ? crypto.randomUUID() : String(Date.now()) + "-" + Math.random().toString(16).slice(2);
    return prefix + "-" + suffix;
  }

  function notice(message, kind) {
    const node = $("notice");
    node.textContent = message;
    node.className = "notice " + (kind || "");
    node.classList.remove("hidden");
  }

  function clearNotice() { $("notice").classList.add("hidden"); }

  function setBusy(value) {
    state.busy = Boolean(value);
    document.querySelectorAll("button").forEach((button) => {
      if (button.id === "refresh" || button.id === "export") return;
      button.disabled = state.busy || READONLY();
    });
  }

  async function api(path, options) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      notice("离线：无法连接本地服务（" + error.message + "）", "error");
      throw error;
    }
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : {};
      const message = (detail.code || response.status) + " · " + (detail.message || "请求失败");
      notice(message, "error");
      throw new Error(message);
    }
    return payload;
  }

  async function write(path, body, success) {
    setBusy(true);
    clearNotice();
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      await refresh();
      notice(success || "已提交", "ok");
      return result;
    } catch (error) {
      return null;
    } finally {
      setBusy(false);
    }
  }

  function kv(label, value) {
    return '<div class="kv"><b>' + esc(label) + "</b>" + esc(value == null || value === "" ? "—" : value) + "</div>";
  }

  function renderConnection() {
    const engine = state.engine || {};
    const online = Boolean(engine.ok);
    $("conn-dot").className = "dot " + (online ? "ok" : "");
    $("conn-text").textContent = online
      ? "已连接 · " + (engine.mode || "unknown") + " · " + (engine.character || "")
      : "未连接";
    $("readonly-badge").classList.toggle("hidden", !engine.read_only);
    $("offline-badge").classList.toggle("hidden", online);
    $("engine-summary").innerHTML = [
      kv("engine mode", engine.mode), kv("root", engine.root),
      kv("card revision", engine.card_revision),
      kv("published version", engine.published_version),
      kv("evolution mode", engine.evolution_mode),
      kv("consumers", Object.keys(engine.consumers || {}).length),
      kv("pending", (engine.pending || []).length),
      kv("failed", (engine.failed || []).length),
    ].join("");
  }

  function renderOverview() {
    const overview = (state.snapshot && state.snapshot.overview) || {};
    const stats = overview.stats || {};
    $("overview").innerHTML = [
      kv("events", stats.events), kv("facts", stats.facts),
      kv("reflections", stats.reflections), kv("growth", stats.growth),
      kv("persona revision", stats.persona_revision),
      kv("version", stats.version), kv("pending proposals", stats.pending_proposals),
    ].join("");
    const timeline = $("timeline");
    const events = (overview.recent_events || []);
    timeline.innerHTML = events.length ? events.map((event) =>
      '<div class="item"><div>' + esc(event.text) + "</div>" +
      '<div class="meta">' + esc(event.occurred_at) + " · " + esc(event.scene) +
      " · " + esc(event.source_key) + "</div></div>"
    ).join("") : '<p class="muted">暂无经历。</p>';
  }

  function renderSearch() {
    const node = $("search-results");
    const result = state.search;
    if (!result) { node.textContent = "输入关键词搜索。"; return; }
    const groups = [
      ["events", "经历"], ["facts", "事实"], ["reflections", "反思"], ["sections", "人格段落"],
    ];
    const rows = [];
    for (const [key, label] of groups) {
      const items = result[key] || [];
      rows.push("<h3>" + label + " (" + items.length + ")</h3>");
      rows.push(items.length ? items.map((item) =>
        '<div class="item"><div>' + esc(item.text || item.section_id) + "</div>" +
        '<div class="meta">' + esc(item.id || item.section_id || "") + "</div></div>"
      ).join("") : '<p class="muted">无结果。</p>');
    }
    node.innerHTML = rows.join("");
  }

  function renderFacts() {
    const snapshot = state.snapshot || {};
    const facts = (snapshot.facts && snapshot.facts.facts) || [];
    $("facts").innerHTML = facts.length ? facts.map((fact) =>
      '<div class="item"><div>' + esc(fact.text) + "</div>" +
      '<div class="meta">' + esc(fact.id) + " · importance " + esc(fact.importance) +
      (fact.absorbed ? " · absorbed" : "") + "</div></div>"
    ).join("") : '<p class="muted">暂无事实。</p>';
    const reflections = (snapshot.reflections && snapshot.reflections.reflections) || [];
    const growth = (snapshot.reflections && snapshot.reflections.growth) || [];
    const rows = reflections.map((row) =>
      '<div class="item"><div>' + esc(row.text) + "</div>" +
      '<div class="meta">' + esc(row.id) + " · " + esc(row.status) +
      " · evidence " + esc(row.evidence_count) + "</div></div>"
    ).concat(growth.map((row) =>
      '<div class="item"><div>' + esc(row.text) + "</div>" +
      '<div class="meta">growth · ' + esc(row.id) + " · " + esc(row.status) + "</div></div>"
    ));
    $("reflections").innerHTML = rows.length ? rows.join("") : '<p class="muted">暂无反思。</p>';
  }

  function renderPersona() {
    const persona = (state.snapshot && state.snapshot.persona) || {};
    const card = persona.card || { sections: [] };
    $("persona-meta").textContent =
      "revision " + (card.revision || 0) + " · initial " + (card.initial_revision || 0) +
      " · published " + ((persona.versions && persona.versions.current && persona.versions.current.version) || "—");
    $("persona-sections").innerHTML = (card.sections || []).map((section) => {
      const editable = section.editable_by && section.editable_by.user && !section.frozen;
      return '<div class="section-edit" data-section="' + esc(section.section_id) + '">' +
        '<div class="row"><strong>' + esc(section.section_id) + "</strong>" +
        '<span class="tag">' + esc(section.kind) + "</span>" +
        (section.frozen ? '<span class="badge">已冻结</span>' : "") +
        '<button class="ghost" data-freeze="' + esc(section.section_id) + '">' +
        (section.frozen ? "解冻" : "冻结") + "</button></div>" +
        (editable
          ? '<textarea data-text="' + esc(section.section_id) + '">' + esc(section.text) + "</textarea>" +
            '<div class="row"><button data-save="' + esc(section.section_id) + '">保存段落</button></div>'
          : '<p>' + esc(section.text) + "</p>") +
        "</div>";
    }).join("");
    const revisions = (persona.revisions || []).slice(0, 12);
    $("persona-history").innerHTML = revisions.length ? revisions.map((row) =>
      '<div class="item"><div>r' + esc(row.revision) + " · " + esc(row.origin) +
      " · " + esc(row.actor) + "</div>" +
      '<div class="meta">' + esc(row.at) + " · " + esc(row.changed_sections.join(", ")) + "</div></div>"
    ).join("") : '<p class="muted">暂无历史。</p>';
    $("persona-projection").textContent = JSON.stringify(persona.projection || {}, null, 2);
    renderConsumers(persona);
  }

  function renderConsumers(persona) {
    const consumers = (persona && persona.consumers) || {};
    const current = persona && persona.versions && persona.versions.current;
    const version = current && current.version;
    const rows = Object.keys(consumers).map((name) => {
      const row = consumers[name] || {};
      return '<div class="item"><div><strong>' + esc(name) + "</strong> · " +
        esc(row.status) + "</div>" +
        '<div class="meta">version ' + esc(row.version || "—") +
        " · attempts " + esc(row.attempts || 0) +
        (row.reason ? " · " + esc(row.reason) : "") + "</div>" +
        (version ? '<div class="row"><button class="ghost" data-adopt="' + esc(name) +
          '" data-version="' + esc(version) + '">标记已采用</button></div>' : "") +
        "</div>";
    });
    $("consumers").innerHTML = rows.length ? rows.join("") : '<p class="muted">投影尚未同步。</p>';
    const pending = (persona && persona.pending) || [];
    const failed = (persona && persona.failed) || [];
    if (pending.length || failed.length) {
      $("consumers").innerHTML += '<p class="muted">pending ' + pending.length +
        " · failed " + failed.length + "</p>";
    }
  }

  function renderPublish() {
    const persona = (state.snapshot && state.snapshot.persona) || {};
    const current = persona.versions && persona.versions.current;
    $("publish-current").textContent = current
      ? "当前版本 " + current.version + " · revision " + current.revision + " · target " + current.target
      : "尚未发布。";
  }

  function renderEvolution() {
    const evolution = (state.snapshot && state.snapshot.evolution) || {};
    $("evolution-mode").value = evolution.mode || "off";
    $("evolution-status").textContent =
      "mode " + (evolution.mode || "off") + " · enabled " + Boolean(evolution.enabled) +
      " · proposals " + ((evolution.proposals || []).length);
    const proposals = evolution.proposals || [];
    $("proposals").innerHTML = proposals.length ? proposals.map((proposal) => {
      const pending = proposal.status === "proposed";
      return '<div class="item"><div><strong>' + esc(proposal.proposal_id) + "</strong> · " +
        esc(proposal.status) + " · base r" + esc(proposal.base_revision) + "</div>" +
        '<div class="meta">' + esc(proposal.reason || "") + "</div>" +
        '<div class="meta">' + (proposal.operations || []).map((op) =>
          esc(op.op) + ":" + esc(op.section_id)).join(" · ") + "</div>" +
        (pending ? '<div class="row">' +
          '<button data-accept="' + esc(proposal.proposal_id) + '">接受</button>' +
          '<button class="ghost" data-reject="' + esc(proposal.proposal_id) + '">拒绝</button>' +
          '<button class="ghost" data-withdraw="' + esc(proposal.proposal_id) + '">撤回</button>' +
          "</div>" : "") + "</div>";
    }).join("") : '<p class="muted">暂无提案。</p>';
  }

  function renderCleanup() {
    const cleanup = (state.snapshot && state.snapshot.cleanup) || {};
    const deletable = cleanup.deletable || [];
    $("deletable").innerHTML = deletable.length ? deletable.map((item) =>
      '<div class="item"><div>' + esc(item.text) + "</div>" +
      '<div class="meta">' + esc(item.type) + " · " + esc(item.id) + "</div>" +
      '<div class="row"><button class="danger" data-deletable="' + esc(item.id) +
      '" data-type="' + esc(item.type) + '">删除</button></div></div>'
    ).join("") : '<p class="muted">暂无可删除条目。</p>';
    const deletions = cleanup.deletions || [];
    $("deletions").innerHTML = deletions.length ? deletions.map((row) =>
      '<div class="item"><div>' + esc(row.id) + " · " + esc(row.status) + "</div>" +
      '<div class="meta">' + esc(row.target_type) + " · " + esc(row.target_id) +
      " · " + esc(row.reason) + "</div></div>"
    ).join("") : '<p class="muted">暂无删除记录。</p>';
    const corrections = cleanup.corrections || [];
    $("corrections").innerHTML = corrections.length ? corrections.map((row) =>
      '<div class="item"><div>' + esc(row.old_text) + " → " + esc(row.new_text) + "</div>" +
      '<div class="meta">' + esc(row.id) + " · " + esc(row.status) + "</div></div>"
    ).join("") : '<p class="muted">暂无更正。</p>';
  }

  function renderRaw() {
    $("raw-json").textContent = JSON.stringify({
      engine: state.engine, snapshot: state.snapshot,
    }, null, 2);
  }

  function render() {
    renderConnection();
    renderOverview();
    renderSearch();
    renderFacts();
    renderPersona();
    renderPublish();
    renderEvolution();
    renderCleanup();
    renderRaw();
    setBusy(state.busy);
  }

  async function refresh() {
    setBusy(true);
    clearNotice();
    try {
      const [snapshot, engine] = await Promise.all([
        api("/api/snapshot"), api("/api/engine/status"),
      ]);
      state.snapshot = snapshot;
      state.engine = engine;
      render();
    } catch (error) {
      $("conn-text").textContent = "连接失败";
      $("offline-badge").classList.remove("hidden");
    } finally {
      setBusy(false);
    }
  }

  function sectionText(sectionId) {
    const persona = state.snapshot && state.snapshot.persona;
    const card = persona && persona.card;
    const section = (card && card.sections || []).find((row) => row.section_id === sectionId);
    return section ? section.text : "";
  }

  function currentRevision() {
    const persona = state.snapshot && state.snapshot.persona;
    return persona && persona.card ? persona.card.revision : 0;
  }

  function currentVersion() {
    const persona = state.snapshot && state.snapshot.persona;
    const current = persona && persona.versions && persona.versions.current;
    return current ? current.version : "";
  }

  function bind() {
    $("refresh").addEventListener("click", refresh);
    $("export").addEventListener("click", () => {
      const anchor = document.createElement("a");
      anchor.href = "/api/export";
      anchor.download = "rin-memory.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    });

    $("search-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = $("search-input").value.trim();
      if (!query) { state.search = null; renderSearch(); return; }
      try {
        state.search = await api("/api/search?q=" + encodeURIComponent(query));
        renderSearch();
      } catch (_) { /* notice already shown */ }
    });
    $("search-clear").addEventListener("click", () => {
      $("search-input").value = ""; state.search = null; renderSearch();
    });

    document.addEventListener("click", async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const dataset = target.dataset || {};

      if (dataset.freeze) {
        await write("/api/persona/freeze", {
          section_id: dataset.freeze,
          frozen: !sectionFrozen(dataset.freeze),
        }, "冻结状态已更新");
      } else if (dataset.save) {
        await write("/api/persona/revise", {
          base_revision: currentRevision(),
          operations: [{
            op: "replace", section_id: dataset.save,
            text: (document.querySelector('[data-text="' + dataset.save + '"]') || {}).value || "",
          }],
          reason: "testbed section edit", request_id: rid("ui-revise"),
        }, "段落已保存");
      } else if (dataset.adopt) {
        await write("/api/persona/adopt", {
          consumer: dataset.adopt, version: dataset.version || currentVersion(),
          receipt_id: rid("ui-adopt"),
        }, "消费者已采用");
      } else if (dataset.accept || dataset.reject || dataset.withdraw) {
        const proposalId = dataset.accept || dataset.reject || dataset.withdraw;
        const action = dataset.accept ? "accept" : dataset.reject ? "reject" : "withdraw";
        await write("/api/evolution/" + encodeURIComponent(proposalId) + "/" + action,
          { reason: "testbed " + action }, "提案 " + action + " 完成");
      } else if (dataset.deletable) {
        await write("/api/cleanup/delete", {
          type: dataset.type, id: dataset.deletable,
          reason: "testbed delete", request_id: rid("ui-delete"),
        }, "删除完成");
      } else if (target.dataset.target) {
        await write("/api/persona/publish", {
          target: target.dataset.target, revision: currentRevision(),
        }, "已发布 " + target.dataset.target);
      }
    });

    $("restore-persona").addEventListener("click", () =>
      write("/api/persona/restore", {}, "已恢复初始卡"));
    $("sync-persona").addEventListener("click", () =>
      write("/api/persona/sync", {}, "投影已同步"));

    $("rollback").addEventListener("click", () => {
      const version = $("rollback-version").value.trim();
      if (!version) { notice("请输入要回滚的 version", "error"); return; }
      write("/api/persona/rollback", { version }, "已回滚");
    });

    $("apply-mode").addEventListener("click", () =>
      write("/api/evolution/mode", { mode: $("evolution-mode").value }, "演化模式已更新"));
    $("evaluate-evolution").addEventListener("click", () =>
      write("/api/evolution/evaluate", {}, "评估完成"));
    $("retry-evolution").addEventListener("click", () =>
      write("/api/evolution/retry", {}, "重试完成"));
    $("propose").addEventListener("click", () => {
      const refs = $("proposal-refs").value.split(",").map((v) => v.trim()).filter(Boolean);
      const sections = $("proposal-sections").value.split(",").map((v) => v.trim()).filter(Boolean);
      write("/api/evolution/propose", {
        base_revision: currentRevision(), source_refs: refs,
        focus_sections: sections, reason: "testbed manual proposal",
        request_id: rid("ui-proposal"),
      }, "提案已提交");
    });

    $("correct").addEventListener("click", () => {
      const oldText = $("correct-old").value.trim();
      const newText = $("correct-new").value.trim();
      if (!oldText && !newText) { notice("旧文本或新文本至少填一个", "error"); return; }
      write("/api/cleanup/correct", {
        old_text: oldText, new_text: newText, request_id: rid("ui-correct"),
      }, "更正已提交");
    });
    $("delete").addEventListener("click", () => {
      const type = $("delete-type").value;
      const id = $("delete-id").value.trim();
      const sourceKey = $("delete-source").value.trim();
      const text = $("delete-text").value.trim();
      if (!id && !sourceKey && !text) { notice("id、source_key 或文本至少填一个", "error"); return; }
      write("/api/cleanup/delete", {
        type: type || undefined, id: id || undefined,
        source_key: sourceKey || undefined, text: text || undefined,
        reason: "testbed delete", request_id: rid("ui-delete"),
      }, "删除请求已提交");
    });
  }

  function sectionFrozen(sectionId) {
    const persona = state.snapshot && state.snapshot.persona;
    const card = persona && persona.card;
    const section = (card && card.sections || []).find((row) => row.section_id === sectionId);
    return Boolean(section && section.frozen);
  }

  bind();
  refresh();
})();
