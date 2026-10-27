/* RIN live layer: same-origin API client for the watercolor console. */
(function () {
  "use strict";
  if (window.__RIN_LIVE_LOADED__) return;
  window.__RIN_LIVE_LOADED__ = true;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
  const rid = (prefix) => prefix + "-" + ((globalThis.crypto && crypto.randomUUID)
    ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2));

  const CLEAN_TYPE_LABELS = { fact: "事实", reflection: "反思", growth: "成长", event: "记忆" };
  const CLEAN_PAGE_SIZE = 10;

  const LIVE = {
    connected: false, snapshot: null, engine: null,
    events: [], proposals: [], selectedProposal: null,
    selectedEvent: null, selectedRevision: null,
    cleanupKind: "all", cleanupLimit: CLEAN_PAGE_SIZE,
    modalConfirm: null,
  };
  window.RIN_LIVE = LIVE;
  const SERVICE = Boolean(window.__RIN_SERVICE__);
  let writeIdentity = null;

  const SECTION_LABELS = {
    identity: "身份", tone: "语气", relationship: "相处", habits: "习惯",
    boundaries: "边界", aesthetic: "审美", interests: "兴趣", speech: "语言习惯",
  };
  const SECTION_CLASS = {
    tone: "tone", relationship: "rel", boundaries: "bound", aesthetic: "hobby",
    habits: "hobby", interests: "hobby",
  };
  const CONSUMER_LABELS = {
    chat: "网页对话", neko_chat: "N.E.K.O 对话", realtime: "实时会话", game: "游戏人格", active: "主动交流",
  };
  const TYPE_CLASS = { "对话": "t-talk", "游戏": "t-game", "更正": "t-fix", "修订": "t-per" };
  const MODE_LABELS = { off: "已关闭", propose: "提议模式", auto: "自动模式" };

  function sectionLabel(id) {
    const key = String(id || "");
    if (SECTION_LABELS[key]) return SECTION_LABELS[key];
    // Real cards imported from the host use host_profile_<中文> / host_override_<中文>.
    const stripped = key.replace(/^host_(profile|override|system_prompt)_/, "");
    return stripped || "段落";
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
      && a.getDate() === b.getDate();
  }

  function dayLabel(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const now = new Date();
    if (sameDay(d, now)) return "今天";
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (sameDay(d, yesterday)) return "昨天";
    return (d.getMonth() + 1) + "." + String(d.getDate()).padStart(2, "0");
  }

  function relTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return dayLabel(iso) + " " + fmtTime(iso);
  }

  function sceneLabel(scene) {
    const text = String(scene || "");
    if (text.startsWith("game:")) return "游戏会话";
    if (text === "plugin:dsh_memory") return "DSH 对话";
    if (text.startsWith("plugin:")) return "插件会话 · " + text.slice(7);
    if (text === "host:chat") return "桌面端对话";
    return text.replace(/^host:/, "").replace(/^plugin:/, "") || "记忆来源";
  }

  function sourceLabel(keys) {
    const key = (keys || [])[0] || "";
    if (key.includes(":user:")) return "对话";
    if (key.includes(":private:")) return "对话";
    if (key.startsWith("plugin:")) return "插件";
    return "引擎";
  }

  function eventType(event) {
    const scene = String(event.scene || "");
    const kind = String(event.kind || "");
    if (scene.startsWith("game:")) return "游戏";
    if (kind === "persona_revision" || kind === "revision") return "修订";
    if (String(event.content_class || "") === "correction") return "更正";
    return "对话";
  }

  function statusTag(status) {
    const map = {
      proposed: ["待审核", "wait"], accepted: ["已采纳", "ok"],
      rejected: ["已驳回", "warn"], withdrawn: ["已撤回", ""],
      active: ["活跃", "ok"], archived: ["已归档", ""], needs_rebuild: ["需重建", "warn"],
    };
    const row = map[status] || [status || "—", ""];
    return '<span class="st ' + row[1] + '">' + esc(row[0]) + "</span>";
  }

  function toast(message, actionLabel, onAction) {
    const node = $("#toast");
    if (!node) return;
    node.innerHTML = "";
    const span = document.createElement("span");
    span.textContent = message;
    node.appendChild(span);
    if (actionLabel) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "t-act";
      button.textContent = actionLabel;
      button.addEventListener("click", () => {
        node.classList.remove("on");
        if (onAction) onAction();
      });
      node.appendChild(button);
    }
    node.classList.add("on");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove("on"), actionLabel ? 5600 : 2800);
  }

  async function api(path, options, retried) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    if (SERVICE) {
      path = path.replace(/^\/api\//, "/v1/");
      if (writeIdentity) Object.assign(init.headers, {
        "X-Companion-Client": writeIdentity.client_id,
        "X-Companion-Token": writeIdentity.write_token,
      });
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (SERVICE && response.status === 401 && !retried && path !== "/v1/bootstrap") {
      writeIdentity = await api("/v1/bootstrap");
      return api(path, options, true);
    }
    if (!response.ok) {
      const detail = (payload && (payload.error || payload)) || {};
      const error = new Error((detail.code || response.status) + " · " + (detail.message || "请求失败"));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function decisionToast(result, success) {
    if (!result) return "未完成";
    if (result.conflict) return "版本冲突，请刷新后重试";
    if (result.status === "rejected") return "未处理：" + (result.reason || "引擎拒绝");
    if (result.duplicate) return "这条记录已经处理过了";
    return success;
  }

  async function write(path, body, success) {
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      if (path.startsWith("/api/persona/")) CHAT.loaded = false;
      await refresh();
      if (success) toast(typeof success === "function" ? success(result) : success);
      return result;
    } catch (error) {
      toast("操作失败：" + error.message);
      return null;
    }
  }

  /* ---------- chrome ---------- */

  function renderChrome() {
    const { meta, overview } = LIVE.snapshot;
    const stats = overview.stats || {};
    const latest = (overview.recent_events || [])[0];
    const navMode = $(".nav-mode");
    if (navMode) navMode.innerHTML = '<i></i>已连接 · 本地引擎';
    const metaNode = $(".mast .meta");
    if (metaNode) {
      metaNode.innerHTML = "第 " + esc(stats.persona_revision || 0) + " 版 · "
        + esc(stats.active_days || 0) + " 天 · <b>" + esc(stats.events || 0) + "</b> 条记忆 · <b>"
        + esc(stats.facts || 0) + "</b> 条事实";
    }
    const footerStatus = $(".foot-right span");
    if (footerStatus) {
      footerStatus.textContent = "最近写入 " + (latest ? relTime(latest.occurred_at) : "—") + " · 本地引擎";
    }
    const topMode = $("#topMode");
    if (topMode) {
      const who = meta.mode === "live" || meta.mode === "service" ? (meta.character || "—") : "演示";
      topMode.textContent = "本地记忆 · " + (meta.read_only ? "只读" : "可写") + " · 角色 " + who;
    }
    const settingsSub = $('[data-page="settings"] .page-sub');
    if (settingsSub) settingsSub.textContent = "本机 · 改动走引擎，不直接写存储";
    document.title = "铃芽 RIN · 记忆管理台";
  }

  /* ---------- overview ---------- */

  function renderOverview() {
    const snapshot = LIVE.snapshot;
    const overview = snapshot.overview || {};
    const stats = overview.stats || {};
    const activity = overview.activity || [];
    const persona = snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const proposals = (snapshot.evolution && snapshot.evolution.proposals) || [];
    const pendingProps = proposals.filter((row) => row.status === "proposed");
    const pendingConsumers = (persona.pending || []).length;
    const todayCount = activity.length ? Number(activity[activity.length - 1].count || 0) : 0;

    const lead = $(".sum-lead .v b");
    if (lead) lead.textContent = todayCount;
    const cells = $(".sum-cells");
    if (cells) {
      cells.innerHTML = [
        ["events", "记忆事件"], ["facts", "沉淀事实"],
        ["reflections", "反思成长"], ["persona_revision", "人格修订"],
      ].map(([key, label]) => '<div><b>' + esc(stats[key] || 0) + "</b><span>" + label + "</span></div>").join("");
    }

    const tide = $("#tide");
    if (tide) {
      const counts = activity.map((row) => Number(row.count || 0));
      const max = Math.max(1, ...counts);
      tide.innerHTML = '<span class="tide-tip" id="tideTip"></span>';
      activity.forEach((row, index) => {
        const bar = document.createElement("button");
        bar.type = "button";
        bar.className = "bar" + (index === activity.length - 1 ? " today" : "");
        bar.style.left = (index * (100 / activity.length)) + "%";
        bar.style.width = (100 / activity.length * 0.68) + "%";
        bar.style.height = Math.max(6, Math.round(Number(row.count || 0) / max * 100)) + "%";
        bar.style.setProperty("--i", index);
        bar.dataset.tip = dayLabel(row.date + "T12:00:00") + " · " + Number(row.count || 0) + " 条";
        bar.setAttribute("aria-label", bar.dataset.tip);
        bar.addEventListener("mouseenter", showTideTip);
        bar.addEventListener("focus", showTideTip);
        bar.addEventListener("mouseleave", hideTideTip);
        bar.addEventListener("blur", hideTideTip);
        tide.appendChild(bar);
      });
    }
    const tideX = $(".tide-x");
    if (tideX) {
      tideX.innerHTML = "<span>21 天前</span><span>今天 · " + todayCount + " 条</span>";
    }

    const attention = $(".attention span");
    if (attention) {
      if (!pendingProps.length && !pendingConsumers) {
        attention.textContent = "暂无待处理事项，记忆在安静地生长。";
      } else {
        attention.innerHTML = "<b>" + pendingProps.length + "</b> 项提案待审核 · <b>"
          + pendingConsumers + "</b> 处发布等待生效";
      }
    }

    renderOverviewPersona(card, persona, pendingProps.length);
    renderRecentEvents(overview.recent_events || []);
    renderPending(pendingProps);
    renderPublishCard(persona);
    renderQuiet(snapshot);
  }

  function showTideTip(event) {
    const tip = $("#tideTip");
    const tide = $("#tide");
    if (!tip || !tide) return;
    const bar = event.currentTarget;
    tip.textContent = bar.dataset.tip || "";
    const barRect = bar.getBoundingClientRect();
    const tideRect = tide.getBoundingClientRect();
    tip.style.left = (barRect.left - tideRect.left + barRect.width / 2) + "px";
    tip.style.top = (barRect.top - tideRect.top) + "px";
    tip.classList.add("on");
  }

  function hideTideTip() {
    const tip = $("#tideTip");
    if (tip) tip.classList.remove("on");
  }

  function renderOverviewPersona(card, persona, pendingCount) {
    const meta = (LIVE.snapshot && LIVE.snapshot.meta) || {};
    const character = meta.mode === "service" ? String(meta.display_name || meta.character || "").trim()
      : meta.mode === "live" ? String(meta.character || "").trim() : "";
    const nm = $(".identity .nm");
    if (nm && character) nm.textContent = character;
    const portrait = $(".identity .portrait img");
    if (portrait && character) portrait.alt = character;
    const mini = $(".topbar .char .mini");
    if (mini && character) mini.alt = character;
    const topChar = $(".topbar .char b");
    if (topChar && character) topChar.textContent = character;
    const sections = card.sections || [];
    const frozen = sections.filter((row) => row.frozen).length;
    const evolvable = sections.filter((row) => !row.frozen && row.editable_by && row.editable_by.evolution).length;
    const sub = $(".identity .sub");
    if (sub) sub.textContent = "当前角色 · 第 " + (card.revision || 0) + " 版 · "
      + (persona.stale ? "待同步到下一轮对话" : "已同步到引擎投影");
    const nums = $(".identity .nums");
    if (nums) {
      nums.innerHTML = "<span>可演化 <b>" + evolvable + "</b></span><span>冻结 <b>" + frozen
        + "</b></span><span class=\"warn\">待处理 <b>" + pendingCount + "</b></span>";
    }
    const secs = $('[data-page="overview"] .secs');
    const wanted = ["tone", "relationship", "boundaries", "aesthetic"];
    let shownSections = wanted.map((id) => sections.find((row) => row.section_id === id)).filter(Boolean);
    if (!shownSections.length) {
      const preferred = ["核心特质", "说话风格", "互动关系", "背景故事"];
      shownSections = preferred
        .map((zh) => sections.find((row) => sectionLabel(row.section_id) === zh))
        .filter(Boolean);
    }
    if (!shownSections.length) {
      shownSections = sections.filter((row) => String(row.text || "").trim()).slice(0, 4);
    }
    if (secs) {
      let rows = shownSections;
      if (!rows.length) {
        // Imported card: prefer the sections that read best as a summary.
        const preferred = ["核心特质", "说话风格", "互动关系", "背景故事"];
        rows = preferred
          .map((zh) => sections.find((row) => sectionLabel(row.section_id) === zh))
          .filter(Boolean);
      }
      if (!rows.length) rows = sections.filter((row) => String(row.text || "").trim()).slice(0, 4);
      secs.innerHTML = rows.map((section) => {
        const cls = SECTION_CLASS[section.section_id] || "";
        const badge = section.frozen
          ? '<span class="badge frz">已冻结 · 自动演化不改动</span>'
          : '<span class="badge">' + (section.editable_by && section.editable_by.evolution ? "可演化" : "固定") + "</span>";
        return '<div class="sec ' + cls + '"><div class="hd"><b>' + esc(sectionLabel(section.section_id))
          + "</b>" + badge + '<span class="right">第 ' + esc(section.revision || 0) + ' 版</span></div><p>'
          + esc(section.text).replace(/\n/g, "<br>") + "</p></div>";
      }).join("");
    }
    const cardHint = $('[data-page="overview"] .col:first-child .card .card-h .hint');
    if (cardHint) cardHint.textContent = "第 " + (card.revision || 0) + " 版 · " + sections.length + " 段";
    const publishHint = $('[data-page="overview"] .col:last-child .card + .card .card-h .hint');
    if (publishHint) publishHint.textContent = "第 " + (card.revision || 0) + " 版";
    const revs = persona.revisions || [];
    const latest = revs[0];
    const revline = $(".revline");
    if (revline) {
      const changed = latest ? (latest.changed_sections || []).map(sectionLabel).join("、") : "";
      revline.innerHTML = '<span class="k">最近修订</span><b>第 ' + esc(latest ? latest.revision : 0)
        + " 版</b><span>" + esc(latest ? relTime(latest.at) : "—") + (changed ? " · " + esc(changed) : "")
        + '</span><a data-nav="persona" href="#persona">全部修订 →</a>';
    }
    const footer = $('[data-page="overview"] .card .card-f .dim');
    if (footer) {
      const shown = new Set(shownSections.map((row) => row.section_id));
      const rest = sections.filter((row) => !shown.has(row.section_id));
      footer.textContent = rest.length ? "其余 " + rest.length + " 段：" + rest.map((row) => sectionLabel(row.section_id)).join(" · ") : "全部段落已在上方展示";
    }
  }

  function renderRecentEvents(events) {
    const list = $("#tlHome");
    const all = $("#tlAllLink");
    const total = Number((LIVE.snapshot.overview.stats || {}).events || events.length);
    if (all) all.textContent = "全部 " + total + " 条";
    const hint = $('[data-page="overview"] .col:first-child .card:nth-child(2) .card-h .hint');
    if (hint) hint.textContent = "最近 " + Math.min(6, events.length) + " 条";
    if (!list) return;
    list.innerHTML = events.slice(0, 6).map((event) => {
      const type = eventType(event);
      return '<article class="tl-row" data-t="' + esc(type) + '" data-live-event="' + esc(event.id) + '">'
        + '<span class="tl-time">' + fmtTime(event.occurred_at) + '</span><span class="tag ' + (TYPE_CLASS[type] || "t-talk") + '">'
        + esc(type) + '</span><div class="tl-body">' + esc(event.text)
        + '<span class="tl-src">' + dayLabel(event.occurred_at) + " · " + esc(sceneLabel(event.scene))
        + ((event.fact_ids || []).length ? " · " + event.fact_ids.length + " 条事实" : "")
        + "</span></div></article>";
    }).join("");
  }

  function proposalTitle(proposal) {
    const operations = proposal.operations || [];
    return Array.from(new Set(operations.map((op) => sectionLabel(op.section_id)))).join("、")
      + " · " + (operations.some((op) => op.op === "replace") ? "修订" : "调整");
  }

  function proposalDiff(proposal) {
    const changes = proposal.changes || (proposal.operations || []).map((op) => ({
      section_id: op.section_id, before_available: false, after: op.op === "delete" ? "" : op.text || "",
    }));
    const text = (change, side) => {
      const value = side === "before" && !change.before_available
        ? "（第 " + proposal.base_revision + " 版原文已不可用）" : change[side] || "（空段落）";
      return (changes.length > 1 ? sectionLabel(change.section_id) + "：\n" : "") + value;
    };
    return { before: changes.map((change) => text(change, "before")).join("\n\n"),
      after: changes.map((change) => text(change, "after")).join("\n\n"),
      section: changes.map((change) => sectionLabel(change.section_id)).join("、"),
      noop: changes.length > 0 && changes.every((change) => change.noop === true) };
  }

  function publicationState(persona) {
    const consumers = persona.consumers || {};
    // `inline` is the service's own conversation: it reads the current
    // published version per turn and has no adoption step to wait on.
    const names = Object.keys(consumers)
      .filter((name) => consumers[name].status !== "not_connected" && consumers[name].status !== "inline");
    const current = (persona.versions && persona.versions.current) || {};
    const adopted = names.filter((name) => current.version && consumers[name].version === current.version && consumers[name].status !== "failed");
    const pending = names.filter((name) => !adopted.includes(name));
    const notices = [];
    if (persona.stale) notices.push(current.version ? "有人格修改尚未发布" : "人格尚未发布");
    if (current.version) pending.forEach((name) => notices.push((CONSUMER_LABELS[name] || name)
      + (consumers[name].status === "failed" ? "采用失败" : "等待采用新版本")));
    return { names, adopted, pending, notice: notices.join("；") };
  }

  function renderPending(proposals) {
    const list = $("#decList");
    const hint = $("#decHint");
    if (!list) return;
    if (!proposals.length) {
      list.innerHTML = '<div class="empty">没有待处理的提案</div>';
      if (hint) hint.textContent = "已全部处理";
    } else {
      list.innerHTML = proposals.slice(0, 2).map((proposal) => {
        const diff = proposalDiff(proposal);
        const same = diff.noop;
        return '<article class="dec" data-live-key="' + esc(proposal.proposal_id) + '">'
          + '<div class="dec-t">' + esc(proposalTitle(proposal)) + "</div>"
          + (same
            ? '<div class="diff"><span class="lab">内容</span><p>' + esc(diff.after) + '</p><span class="lab">提示</span><p class="dim" style="color:var(--ink-3)">提案修改前后完全一致，采纳不会改变文本。</p></div>'
            : '<div class="diff"><span class="lab">修改前</span><p><del>' + esc(diff.before) + "</del></p>"
              + '<span class="lab">改为</span><p><ins>' + esc(diff.after) + "</ins></p></div>")
          + '<div class="dec-src">来源 · ' + esc(proposal.reason || "演化提案") + " · " + relTime(proposal.created_at) + "</div>"
          + '<div class="dec-act">'
          + (same ? "" : '<button type="button" class="btn pri" data-live-prop-accept="' + esc(proposal.proposal_id)
            + '">采纳</button>')
          + '<button type="button" class="btn' + (same ? " pri" : "") + '" data-live-prop-reject="' + esc(proposal.proposal_id)
          + '">' + (same ? "驳回这条空提案" : "驳回") + '</button><span class="ago">'
          + relTime(proposal.created_at) + "</span></div></article>";
      }).join("");
      if (hint) hint.innerHTML = "显示 " + Math.min(2, proposals.length) + " 项 / 共 " + proposals.length + " 项";
    }
    const showAll = $("#showAll");
    if (showAll) showAll.textContent = proposals.length ? "查看全部 " + proposals.length + " 项 →" : "已全部处理";
    const pend = $("#pendNum");
    if (pend) pend.textContent = proposals.length;
    const badge = $("#evoBadge");
    if (badge) badge.textContent = proposals.length || "";
    const attn = $("#attnProp");
    if (attn) attn.textContent = proposals.length;
  }

  function renderPublishCard(persona) {
    const consumers = persona.consumers || {};
    const current = (persona.versions && persona.versions.current) || {};
    const publication = publicationState(persona);
    const names = publication.names;
    const adopted = names.filter((name) => {
      const row = consumers[name] || {};
      return row.version && row.version === current.version && row.status !== "failed";
    }).length;
    const total = Math.max(names.length, 1);
    const big = $('[data-page="overview"] .pub-big b');
    if (big) big.textContent = adopted + "/" + total;
    const bar = $('[data-page="overview"] .pub-bar');
    if (bar) {
      bar.innerHTML = Array.from({ length: total }, (_, index) =>
        '<i class="' + (index < adopted ? "ok" : "wait") + '"></i>').join("");
    }
    const cardFooter = $('[data-page="overview"] .col:last-child .card + .card .card-f');
    if (cardFooter) {
      const history = (persona.versions && persona.versions.history) || [];
      const previous = history[1];
      cardFooter.innerHTML = (previous ? '<a data-nav="publish" href="#publish">回滚点 · 第 ' + esc(previous.revision) + ' 版</a>' : '<span class="dim">暂无回滚点</span>')
        + '<a data-drawer="publish" href="#publish">查看详情 →</a>';
    }
    const wait = $('[data-page="overview"] .pub-wait');
    if (wait) {
      const pending = names.filter((name) => {
        const row = consumers[name] || {};
        return !(row.version && row.version === current.version);
      });
      wait.innerHTML = '<i></i>' + (pending.length
        ? esc(CONSUMER_LABELS[pending[0]] || pending[0]) + " · 等待采用<b>" + relTime(current.published_at) + "</b>"
        : "已接入的使用方均已采用<b>" + relTime(current.published_at) + "</b>");
    }
  }

  function renderQuiet(snapshot) {
    const block = $(".quiet-block");
    if (!block) return;
    const reflections = (snapshot.reflections.reflections || []).slice(0, 1);
    const growth = (snapshot.reflections.growth || []).slice(0, 1);
    const rows = [];
    reflections.forEach((row) => {
      rows.push('<div class="quiet-row"><span class="tag ' + (row.promoted ? "t-talk" : "t-fix") + '">'
        + (row.promoted ? "已晋升" : "待晋升") + "</span><p>" + esc(row.text) + '</p><span class="t">'
        + dayLabel(row.created_at) + "</span></div>");
    });
    growth.forEach((row) => {
      rows.push('<div class="quiet-row"><span class="tag t-game">成长</span><p>' + esc(row.text)
        + '</p><span class="t">' + dayLabel(row.created_at) + "</span></div>");
    });
    const header = block.querySelector(".quiet-h");
    block.innerHTML = (header ? header.outerHTML : "") + (rows.join("") || '<div class="empty">暂无反思</div>');
  }

  /* ---------- timeline ---------- */

  function renderTimeline() {
    const events = (LIVE.snapshot.timeline && LIVE.snapshot.timeline.events) || [];
    LIVE.events = events;
    window.RIN_TOTAL = events.length;
    const title = $("#tlTitle");
    if (title) title.textContent = "时间流 · " + events.length + " 条";
    const sub = $('[data-page="timeline"] .page-sub');
    if (sub) sub.textContent = events.length
      ? "最近写入 " + relTime(events[0].occurred_at) + " · 按时间倒序" : "还没有写入记忆";

    const chips = $('[data-page="timeline"] .toolbar .chips');
    if (chips) {
      const counts = {};
      events.forEach((event) => {
        const type = eventType(event);
        counts[type] = (counts[type] || 0) + 1;
      });
      const order = ["对话", "游戏", "更正", "修订"].filter((type) => counts[type]);
      chips.innerHTML = '<button type="button" class="chip on" data-filter="全部">全部 <span class="n">'
        + events.length + "</span></button>" + order.map((type) =>
          '<button type="button" class="chip" data-filter="' + type + '">' + type
          + ' <span class="n">' + counts[type] + "</span></button>").join("");
    }

    const list = $('[data-page="timeline"] .tl');
    if (list) {
      const groups = new Map();
      events.forEach((event) => {
        const label = dayLabel(event.occurred_at);
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(event);
      });
      const html = [];
      groups.forEach((rows, label) => {
        html.push('<div class="tl-day">' + esc(label) + "<span>" + rows.length + " 条</span></div>");
        rows.forEach((event) => {
          const type = eventType(event);
          html.push('<article class="tl-row filterable" data-t="' + esc(type)
            + '" data-time="' + relTime(event.occurred_at) + '" data-src="' + esc(sceneLabel(event.scene))
            + '" data-id="' + esc(event.id) + '" data-full="' + esc(event.text)
            + '" data-source="' + esc(event.source_key) + '" data-live-event="' + esc(event.id) + '">'
            + '<span class="tl-time">' + fmtTime(event.occurred_at) + '</span><span class="tag '
            + (TYPE_CLASS[type] || "t-talk") + '">' + esc(type) + '</span><div class="tl-body">'
            + esc(event.text) + '<span class="tl-src">' + dayLabel(event.occurred_at) + " · "
            + esc(sceneLabel(event.scene)) + ((event.fact_ids || []).length ? " · " + event.fact_ids.length + " 条事实" : "")
            + ((event.reflection_ids || []).length ? " · " + event.reflection_ids.length + " 条反思" : "")
            + "</span></div></article>");
        });
      });
      list.innerHTML = html.join("")
        + '<div class="empty" id="tlEmpty"' + (html.length ? " hidden" : "") + '>'
        + (html.length ? "没有匹配的记忆" : "引擎还没有记录到记忆 · 新对话开始后会出现在这里")
        + "</div>";
    }
    const pager = $('[data-page="timeline"] .pager');
    if (pager) pager.style.display = "none";
    const sum = $("#tlSum");
    const foot = $("#tlFoot");
    if (sum) sum.textContent = "共 " + events.length + " 条";
    if (foot) foot.textContent = "共 " + events.length + " 条";
    if (events.length) {
      const first = $('[data-page="timeline"] .tl-row');
      if (first) selectLiveEvent(first.dataset.liveEvent);
    }
  }

  function selectLiveEvent(eventId) {
    const events = LIVE.events || [];
    const event = events.find((row) => row.id === eventId);
    if (!event) return;
    LIVE.selectedEvent = event;
    $$('[data-page="timeline"] .tl-row').forEach((row) => {
      row.classList.toggle("selected", row.dataset.liveEvent === eventId);
    });
    const type = eventType(event);
    const tag = $("#dtTag");
    if (tag) { tag.className = "tag " + (TYPE_CLASS[type] || "t-talk"); tag.textContent = type; }
    const full = $("#dtFull"); if (full) full.textContent = event.text;
    const time = $("#dtTime"); if (time) time.textContent = relTime(event.occurred_at);
    const src = $("#dtSrc"); if (src) src.textContent = sceneLabel(event.scene);
    const typeNode = $("#dtType"); if (typeNode) typeNode.textContent = type;
    const id = $("#dtId"); if (id) id.textContent = event.id;
    const state = $("#dtState");
    if (state) {
      const nFact = (event.fact_ids || []).length;
      const nRefl = (event.reflection_ids || []).length;
      if (nFact) {
        state.textContent = "已沉淀为事实" + (nRefl ? " · 已参与 " + nRefl + " 条反思" : "");
      } else if (nRefl) {
        state.textContent = "已参与 " + nRefl + " 条反思";
      } else {
        // 抽取已完成且确实无可记内容（如问句），不是"还在等"
        state.textContent = "已分析 · 无客户记为事实";
      }
    }
    const rel = $("#dtRel");
    if (rel) {
      const facts = (LIVE.snapshot.facts.facts || []).filter((row) => (event.fact_ids || []).includes(row.id));
      const reflections = (LIVE.snapshot.reflections.reflections || []).filter((row) => (event.reflection_ids || []).includes(row.id));
      const rows = facts.map((row) => '<div class="rel-item"><span class="tag t-fix">事实</span>'
        + esc(row.text) + "<b>" + esc(row.id) + "</b></div>")
        .concat(reflections.map((row) => '<div class="rel-item"><span class="tag t-talk">反思</span>'
          + esc(row.text) + "<b>" + esc(row.id) + "</b></div>"));
      rel.innerHTML = rows.length ? rows.join("") : '<div class="rel-item"><span class="tag">无</span>已分析，这条经历没有可沉淀的事实或反思</div>';
    }
  }

  /* ---------- facts ---------- */

  function renderFacts() {
    const facts = (LIVE.snapshot.facts && LIVE.snapshot.facts.facts) || [];
    const title = $('[data-page="facts"] .page-title');
    if (title) title.textContent = "事实 · " + facts.length + " 条";
    const sub = $('[data-page="facts"] .page-sub');
    const absorbed = facts.filter((row) => row.absorbed).length;
    if (sub) sub.textContent = absorbed + " 条已吸收 · " + (facts.length - absorbed) + " 条待吸收 · 事实由引擎抽取";

    const chips = $('[data-page="facts"] .toolbar .chips');
    if (chips) {
      chips.innerHTML = [
        ["全部", facts.length], ["已吸收", absorbed], ["未吸收", facts.length - absorbed],
      ].map(([label, count], index) => '<button type="button" class="chip' + (index === 0 ? " on" : "")
        + '" data-status="' + label + '">' + label + ' <span class="n">' + count + "</span></button>").join("");
    }
    const body = $('[data-page="facts"] tbody');
    if (body) {
      body.innerHTML = facts.map((fact) => {
        const status = fact.absorbed ? "已吸收" : "未吸收";
        return '<tr data-status="' + status + '"><td class="content">' + esc(fact.text)
          + '</td><td><span class="st ' + (fact.absorbed ? "ok" : "wait") + '">' + status
          + '</span></td><td class="dim">' + sourceLabel(fact.source_keys) + " · " + dayLabel(fact.created_at)
          + '</td><td class="num">' + Number(fact.confidence || 0).toFixed(2)
          + '</td><td class="mono">' + esc(fact.id) + "</td></tr>";
      }).join("");
    }
    const tools = $('[data-page="facts"] .page-tools');
    if (tools) tools.innerHTML = '<a class="btn" href="/api/export" download="rin-memory.json">导出 JSON</a>';
    const footer = $('[data-page="facts"] .card-f');
    if (footer) footer.innerHTML = '<span class="dim">置信度来自引擎抽取 · 事实只读</span><a href="/api/export" download="rin-memory.json">导出 JSON →</a>';
  }

  /* ---------- reflections ---------- */

  function renderReflections() {
    const reflections = (LIVE.snapshot.reflections.reflections || []);
    const growth = (LIVE.snapshot.reflections.growth || []);
    const title = $('[data-page="reflections"] .page-title');
    if (title) title.textContent = "反思 · " + reflections.length + " 条";
    const pending = reflections.filter((row) => !row.promoted).length;
    const sub = $("#reflSub");
    if (sub) sub.textContent = pending + " 条待晋升 · " + growth.length + " 条成长 · 晋升由引擎维护循环处理";
    const hint = $("#reflListHint");
    if (hint) hint.textContent = reflections.length + " 条反思 · " + growth.length + " 条成长";

    const list = $('[data-page="reflections"] .panel .card-b[style*="padding:0"]');
    if (list) {
      const rows = reflections.map((row, index) => {
        const status = row.promoted ? "已晋升" : row.needs_rebuild ? "需重建" : "待晋升";
        const cls = row.promoted ? "st ok" : row.needs_rebuild ? "st warn" : "st wait";
        return '<div class="refl-row' + (index === 0 ? " selected" : "") + '" data-r="live-' + index
          + '" data-live-refl="' + esc(row.id) + '"><h3>' + esc(row.text) + "</h3><p>证据 "
          + esc(row.evidence_count || 0) + " 条 · 强度 " + esc(row.evidence_strength || 0)
          + '</p><div class="meta"><span class="tag t-talk">证据 ×' + esc(row.evidence_count || 0)
          + '</span><span class="' + cls + '">' + status + "</span></div></div>";
      }).concat(growth.map((row, index) =>
        '<div class="refl-row" data-r="live-g' + index + '" data-live-growth="' + esc(row.id)
        + '"><h3>' + esc(row.text) + '</h3><p>成长 · 支持 ' + esc(row.support_count || 0)
        + ' 条来源</p><div class="meta"><span class="tag t-game">成长</span><span class="st ok">'
        + esc({ active: "生效中", archived: "已归档", withdrawn: "已撤回" }[row.status] || row.status || "生效中")
        + "</span></div></div>")).join("");
      list.innerHTML = rows || '<div class="empty">暂无反思</div>';
    }
    if (reflections.length) selectLiveReflection(reflections[0].id);
    else if (growth.length) selectLiveReflection(growth[0].id);
  }

  function selectLiveReflection(reflectionId) {
    const reflections = LIVE.snapshot.reflections.reflections || [];
    const growth = LIVE.snapshot.reflections.growth || [];
    const row = reflections.find((item) => item.id === reflectionId);
    const grow = growth.find((item) => item.id === reflectionId);
    if (!row && !grow) return;
    $$('[data-page="reflections"] .refl-row').forEach((node) => {
      node.classList.toggle("selected", node.dataset.liveRefl === reflectionId || node.dataset.liveGrowth === reflectionId);
    });
    const hint = $("#reflHint");
    const title = $("#reflTitle");
    const reason = $("#reflReason");
    const ev = $("#reflEv");
    const suggest = $("#reflSuggest");
    const foot = $("#reflFoot");
    const actions = $('[data-page="reflections"] .detail-act');
    if (grow) {
      if (hint) hint.textContent = grow.id + " · 成长";
      if (title) title.textContent = grow.text;
      if (reason) reason.textContent = "这条成长来自 " + (grow.support_count || 0) + " 条独立来源，已进入人格投影。";
      if (ev) ev.innerHTML = (grow.source_refs || []).map((key) =>
        '<tr><td>' + esc(key) + '</td><td class="dim">—</td><td><span class="strength lv3"><i class="on"></i><i class="on"></i><i class="on"></i></span></td></tr>').join("");
      if (suggest) suggest.textContent = "成长条目由引擎维护；如需删除，请到清理页处理。";
      if (foot) foot.textContent = "支持 " + (grow.support_count || 0) + " 条来源";
      if (actions) actions.innerHTML = '<a class="btn ghost" data-nav="cleanup" href="#cleanup">去清理 →</a>';
      return;
    }
    const status = row.promoted ? "已晋升" : row.needs_rebuild ? "需重建" : "待晋升";
    if (hint) hint.textContent = row.id + " · " + status;
    if (title) title.textContent = row.text;
    if (reason) reason.textContent = "来自 " + (row.evidence_count || 0) + " 条来源 · 证据强度 " + (row.evidence_strength || 0);
    if (ev) ev.innerHTML = (row.evidence || []).map((item) => {
      const level = Math.max(1, Math.min(3, Math.round(Number(item.weight || 1) * 2)));
      const bars = [1, 2, 3].map((n) => '<i class="' + (n <= level ? "on" : "") + '"></i>').join("");
      return '<tr><td>' + esc(item.source_key) + '</td><td class="dim">权重 ' + esc(item.weight)
        + '</td><td><span class="strength lv' + level + '">' + bars + "</span></td></tr>";
    }).join("") || '<tr><td colspan="3" class="dim">暂无证据</td></tr>';
    if (suggest) suggest.textContent = row.promoted
      ? "这条反思已经晋升为成长，并参与人格投影。"
      : row.needs_rebuild ? "来源被更正或删除，引擎会在维护循环中重建这条反思。" : "证据达到阈值后，引擎会在维护循环中晋升这条反思。";
    if (foot) foot.textContent = "证据 " + (row.evidence_count || 0) + " 条 · 强度 " + (row.evidence_strength || 0);
    if (actions) actions.innerHTML = '<span class="dim" style="font-size:13px">晋升由引擎维护循环处理</span>'
      + '<a class="btn ghost" data-nav="evolution" href="#evolution">查看演化 →</a>';
  }

  /* ---------- persona ---------- */

  function uniqueVersions(rows) {
    const seen = new Set();
    return rows.filter((row) => {
      const key = String(row.version || "");
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function renderPersona() {
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const sections = card.sections || [];
    const revisions = persona.revisions || [];
    const historyHint = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-h .hint');
    if (historyHint) historyHint.textContent = revisions.length + " 个版本";
    const frozen = sections.filter((row) => row.frozen).length;
    const evolvable = sections.filter((row) => !row.frozen && row.editable_by && row.editable_by.evolution).length;
    const title = $('[data-page="persona"] .page-title');
    if (title) title.textContent = "人格卡 · 第 " + (card.revision || 0) + " 版";
    const sub = $('[data-page="persona"] .page-sub');
    if (sub) sub.textContent = sections.length + " 段 · " + evolvable + " 可演化 / " + frozen + " 冻结 · 编辑会创建新版本";
    const secHint = $("#secHint");
    if (secHint) secHint.textContent = "第 " + (card.revision || 0) + " 版 · 冻结段不可被自动演化覆盖";
    const tools = $('[data-page="persona"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-refresh>刷新数据</button>'
      + '<button type="button" class="btn pri" data-live-edit-persona>编辑人格卡</button>';

    const grid = $('[data-page="persona"] .secs.wide');
    if (grid) {
      grid.innerHTML = sections.map((section) => {
        const cls = SECTION_CLASS[section.section_id] || "";
        const badge = section.frozen
          ? '<span class="badge frz">已冻结 · 自动演化不改动</span>'
          : '<span class="badge">' + (section.editable_by && section.editable_by.evolution ? "可演化" : "固定") + "</span>";
        return '<div class="sec ' + cls + '"><div class="hd"><b>' + esc(sectionLabel(section.section_id))
          + "</b>" + badge + '<span class="right">第 ' + esc(section.revision || 0) + ' 版</span></div><p>'
          + esc(section.text).replace(/\n/g, "<br>") + '</p><div class="sec-actions">'
          + '<button type="button" data-live-edit="' + esc(section.section_id) + '">编辑</button>'
          + '<button type="button" data-live-freeze="' + esc(section.section_id) + '" data-frozen="' + (section.frozen ? "1" : "0") + '">'
          + (section.frozen ? "解冻" : "冻结") + "</button></div></div>";
      }).join("");
    }
    const list = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-b');
    if (list) {
      const rows = revisions.map((revision, index) => {
        const changed = (revision.changed_sections || []).map(sectionLabel).join("、") || "—";
        const published = persona.versions && persona.versions.current;
        const state = published && revision.revision === published.revision ? "已发布"
          : revision.revision === card.revision ? "待发布" : revision.origin === "imported" ? "初始版本" : "已归档";
        return '<div class="ver-row' + (index === 0 ? " selected" : "") + '" data-v="live-' + revision.revision
          + '" data-live-revision="' + revision.revision + '"><span class="v">第 ' + revision.revision
          + ' 版</span><span class="c">' + esc(changed) + " · " + esc(revision.origin || "user_edit")
          + '</span><span class="s">' + state + "</span></div>";
      }).join("");
      list.innerHTML = rows + '<div class="diff-panel" id="liveDiffPanel"></div>';
    }
    const history = uniqueVersions((persona.versions && persona.versions.history) || []);
    const footer = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-f');
    if (footer) {
      const previous = history[1];
      footer.innerHTML = '<a data-nav="publish" href="#publish">发布与生效 →</a>'
        + (previous ? '<a data-live-rollback="' + esc(previous.version) + '">回滚到上一版</a>' : '<span class="dim">暂无回滚点</span>');
    }
    if (revisions.length) showLiveRevision(revisions[0].revision);
  }

  function showLiveRevision(revisionNumber) {
    const persona = LIVE.snapshot.persona || {};
    const revisions = persona.revisions || [];
    const revision = revisions.find((row) => row.revision === Number(revisionNumber));
    const panel = $("#liveDiffPanel");
    if (!revision || !panel) return;
    $$('[data-page="persona"] .ver-row').forEach((node) => {
      node.classList.toggle("selected", Number(node.dataset.liveRevision) === Number(revisionNumber));
    });
    const ops = revision.operations || [];
    const changed = (revision.changed_sections || []).map(sectionLabel).join("、") || "—";
    panel.innerHTML = '<div class="h"><b>第 ' + revision.revision + " 版</b><span>"
      + esc(revision.origin || "user_edit") + " · " + relTime(revision.at) + "</span></div>"
      + '<div class="row"><span class="lab">段落</span><p>' + esc(changed) + "</p></div>"
      + '<div class="row"><span class="lab">说明</span><p>' + esc(revision.reason || "—") + "</p></div>"
      + ops.slice(0, 3).map((op) => '<div class="row"><span class="lab">' + esc(op.op === "replace" ? "改为" : op.op)
        + "</span><p>" + esc(op.text || "—") + "</p></div>").join("");
  }

  /* ---------- publish ---------- */

  function renderPublish() {
    const persona = LIVE.snapshot.persona || {};
    const raw = persona.versions && persona.versions.current;
    const current = raw && typeof raw === "object" ? raw : null;
    const currentVersion = current ? String(current.version || "") : "";
    const tokenHint = $('[data-page="publish"] .panel .card-h .hint');
    if (tokenHint) tokenHint.textContent = "版本令牌 " + (currentVersion || "尚未发布");
    const history = uniqueVersions((persona.versions && persona.versions.history) || []);
    const consumers = persona.consumers || {};
    const names = publicationState(persona).names;
    const adopted = currentVersion ? names.filter((name) => {
      const row = consumers[name] || {};
      return row.version && row.version === currentVersion && row.status !== "failed";
    }).length : 0;
    const total = Math.max(names.length, 1);
    const title = $('[data-page="publish"] .page-title');
    if (title) title.textContent = current ? "发布与生效 · 第 " + current.revision + " 版" : "发布与生效";
    const sub = $('[data-page="publish"] .page-sub');
    if (sub) sub.textContent = current
      ? adopted + "/" + total + " 已生效 · 发布于 " + relTime(current.published_at)
      : "尚未发布";
    const tools = $('[data-page="publish"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-publish="next_turn">发布到下一轮</button>';
    const historyHint = $("#pubHistoryHint");
    if (historyHint) historyHint.textContent = history.length > 1 ? (history.length - 1) + " 个可用回滚点" : "暂无回滚点";
    const actionHint = $("#pubActionHint");
    if (actionHint) actionHint.textContent = current ? "第 " + current.revision + " 版" : "尚未发布";

    const card = $('[data-page="publish"] .panel .card-b');
    if (card) {
      const rows = Object.keys(consumers).map((name) => {
        const row = consumers[name] || {};
        const disconnected = row.status === "not_connected";
        const inline = row.status === "inline";
        const done = currentVersion && row.version === currentVersion && row.status !== "failed";
        const saved = !currentVersion && row.status === "saved" && row.version;
        const state = disconnected ? "未接入" : inline && !done ? "本机对话" : done ? "已采用" : saved ? "已保存" : row.version ? "待生效" : "待同步";
        const detail = disconnected ? "接入后报告版本采用状态" : inline && !done ? "每轮读取当前发布版本，无需采用"
          : done ? "版本 " + esc(String(row.version).slice(0, 8))
            : saved
              ? "等待第一次发布"
              : row.version ? "版本 " + esc(String(row.version).slice(0, 8)) : "尚未采用任何版本";
        return '<div class="consumer' + (disconnected || (inline && !done) ? " offline" : done || saved ? "" : " wait") + '"><div class="n"><i></i>'
          + esc(CONSUMER_LABELS[name] || name) + '</div><div class="st">' + state + '</div><div class="t">'
          + detail + (row.reason ? " · " + esc(row.reason) : "") + "</div></div>";
      }).join("");
      card.innerHTML = '<div class="pub-top"><div class="pub-big"><b>' + adopted + "/" + total
        + '</b><span>' + (currentVersion ? "使用方已采用当前版本" : "尚未发布任何版本") + '</span></div><div class="pub-bar" style="width:300px;margin-top:0">'
        + Array.from({ length: total }, (_, index) => '<i class="' + (index < adopted ? "ok" : "wait") + '"></i>').join("")
        + '</div></div><div class="consumers">' + rows + "</div>";
    }
    const table = $('[data-page="publish"] .tbl tbody');
    if (table) {
      table.innerHTML = history.map((row) => '<tr><td class="mono">第 ' + esc(row.revision)
        + '</td><td class="mono" title="' + esc(row.version) + '">' + esc(String(row.version || "").slice(0, 8))
        + '</td><td class="dim">' + esc(relTime(row.published_at))
        + '</td><td>' + (currentVersion && row.version === currentVersion ? '<span class="st ok">生效中</span>'
          : row.version === (history[1] && history[1].version) ? "回滚点" : "已归档") + "</td></tr>").join("");
    }
    const actionPanel = $('[data-page="publish"] .action-panel .card-b');
    if (actionPanel) {
      const previous = history[1];
      actionPanel.innerHTML = (previous ? '<button type="button" class="btn pri" style="width:100%" data-live-rollback="'
        + esc(previous.version) + '">回滚到上一版</button>' : "")
        + '<button type="button" class="btn" style="width:100%" data-live-publish="realtime">发布到实时会话</button>'
        + '<button type="button" class="btn" style="width:100%" data-live-publish="game">发布到游戏人格</button>'
        + '<div class="token-row"><span>版本令牌</span><b title="'
        + esc(current ? current.version : "") + '">' + esc(current ? String(current.version).slice(0, 8) : "—") + "</b></div>"
        + '<div class="token-row"><span>版本修订</span><b>r' + esc(current ? current.revision : "—") + "</b></div>"
        + '<div class="token-row"><span>发布于</span><b>' + esc(current ? relTime(current.published_at) : "—") + "</b></div>"
        + '<p class="note">回滚会重新下发上一版，并保留切换后新增的对话、更正与删除记录。</p>';
    }
    const footer = $('[data-page="publish"] .panel .card-f');
    if (footer) footer.innerHTML = '<span class="dim">只统计已接入的使用方；未接入项不会产生发布提醒</span><a data-live-refresh>刷新状态</a>';
  }

  /* ---------- evolution ---------- */

  function renderEvolution() {
    const evolution = LIVE.snapshot.evolution || {};
    const proposals = evolution.proposals || [];
    LIVE.proposals = proposals;
    const pending = proposals.filter((row) => row.status === "proposed");
    const title = $("#evoTitle");
    if (title) title.textContent = "演化 · " + proposals.length + " 项";
    const sub = $("#evoSub");
    if (sub) sub.textContent = (MODE_LABELS[evolution.mode] || evolution.mode) + " · " + pending.length + " 项待审核";
    const hint = $("#propListHint");
    if (hint) hint.textContent = proposals.length + " 项 · " + pending.length + " 项待审核";
    const chips = $("#modeChips");
    if (chips) {
      chips.innerHTML = ["off", "propose", "auto"].map((mode) =>
        '<button type="button" class="chip' + (evolution.mode === mode ? " on" : "")
        + '" data-live-mode="' + mode + '">' + ({ off: "关闭", propose: "提议", auto: "自动" }[mode]) + "</button>").join("");
    }
    const list = $('[data-page="evolution"] .panel .card-b[style*="padding:0"]');
    if (list) {
      list.innerHTML = proposals.map((proposal, index) => {
        const diff = proposalDiff(proposal);
        return '<div class="prop-row' + (index === 0 ? " selected" : "") + '" data-prop="live-' + index
          + '" data-live-proposal="' + esc(proposal.proposal_id) + '"><h3>' + esc(proposalTitle(proposal))
          + '</h3><div class="meta"><span>基础 r' + esc(proposal.base_revision || 0) + "</span><span>"
          + esc(diff.section) + '</span><span class="st ' + (proposal.status === "proposed" ? "wait" : proposal.status === "accepted" ? "ok" : "warn")
          + '">' + ({ proposed: "待审核", accepted: "已采纳", rejected: "已驳回", withdrawn: "已撤回" }[proposal.status] || proposal.status)
          + "</span></div></div>";
      }).join("") || '<div class="empty">暂无提案</div>';
    }
    const note = $("#evoNote");
    if (note) note.textContent = evolution.mode === "auto" ? "自动模式只处理可演化段，冻结段不受影响" : "自动模式需要人工开启";
    if (proposals.length) selectLiveProposal(proposals[0].proposal_id);
  }

  function selectLiveProposal(proposalId) {
    const proposal = (LIVE.proposals || []).find((row) => row.proposal_id === proposalId);
    if (!proposal) return;
    LIVE.selectedProposal = proposal;
    $$('[data-page="evolution"] .prop-row').forEach((node) => {
      node.classList.toggle("selected", node.dataset.liveProposal === proposalId);
    });
    const diff = proposalDiff(proposal);
    const hint = $("#propHint");
    if (hint) hint.textContent = proposal.proposal_id + " · " + ({ proposed: "待审核", accepted: "已采纳", rejected: "已驳回", withdrawn: "已撤回" }[proposal.status] || proposal.status);
    const title = $("#propTitle");
    if (title) title.textContent = proposalTitle(proposal);
    const before = $("#propBefore");
    if (before) before.innerHTML = "<del>" + esc(diff.before) + "</del>";
    const after = $("#propAfter");
    if (after) after.innerHTML = "<ins>" + esc(diff.after) + "</ins>";
    const ev = $("#propEv");
    if (ev) {
      const evidence = proposal.evidence || {};
      const facts = (LIVE.snapshot.facts.facts || []).filter((row) => (evidence.fact_ids || []).includes(row.id));
      const reflections = (LIVE.snapshot.reflections.reflections || []).filter((row) => (evidence.reflection_ids || []).includes(row.id));
      ev.innerHTML = facts.map((row) => '<div class="ev"><p>' + esc(row.text) + '</p><div class="m"><span>事实 · '
        + esc(row.id) + "</span><span>强</span></div></div>")
        .concat(reflections.map((row) => '<div class="ev"><p>' + esc(row.text) + '</p><div class="m"><span>反思 · '
          + esc(row.id) + "</span><span>中</span></div></div>")).join("")
        || '<div class="ev"><p>这条提案没有附证据。</p></div>';
    }
    const noopNote = $("#propNoop");
    if (noopNote) noopNote.hidden = proposal.status !== "proposed" || !diff.noop;
    const impact = $("#propImpact");
    if (impact) impact.innerHTML = '<span class="tag t-per">' + esc(diff.section) + "</span><span class=\"tag t-talk\">基于第 "
      + esc(proposal.base_revision || 0) + " 版</span>"
      + (proposal.accepted_revision ? '<span class="tag t-talk">已写入第 ' + esc(proposal.accepted_revision) + " 版</span>" : "");
    const actions = $('[data-page="evolution"] .detail-act');
    if (actions) {
      const open = proposal.status === "proposed";
      const noop = diff.noop;
      actions.innerHTML = (noop ? "" : '<button type="button" class="btn pri" data-live-prop-accept="'
        + esc(proposal.proposal_id) + '"' + (open ? "" : " disabled") + ">采纳</button>")
        + '<button type="button" class="btn" data-live-prop-reject="' + esc(proposal.proposal_id) + '"'
        + (open ? "" : " disabled") + ">驳回</button>"
        + '<button type="button" class="btn ghost" data-live-prop-withdraw="' + esc(proposal.proposal_id) + '"'
        + (open ? "" : " disabled") + ">撤回提案</button>";
    }
  }

  /* ---------- cleanup ---------- */

  function renderCleanup() {
    const cleanup = LIVE.snapshot.cleanup || {};
    const deletable = cleanup.deletable || [];
    const deletions = cleanup.deletions || [];
    const corrections = cleanup.corrections || [];
    const pendingDeletions = deletions.filter((row) => row.status !== "completed");
    const title = $('[data-page="cleanup"] .page-title');
    if (title) title.textContent = "清理 · " + deletable.length + " 项可清理"
      + (pendingDeletions.length ? " · " + pendingDeletions.length + " 项处理中" : "");
    const sub = $('[data-page="cleanup"] .page-sub');
    if (sub) sub.textContent = "删除与更正立即停止使用相关内容，清理进度在此查看";
    const tools = $('[data-page="cleanup"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-tombstones>查看墓碑</button>';
    const cleanHint = $("#cleanHint");
    if (cleanHint) cleanHint.textContent = deletable.length + " 项可清理 · " + pendingDeletions.length + " 项处理中";

    // type filter + page slice keep the queue readable as the root grows
    const kinds = Array.from(new Set(deletable.map((item) => item.type)));
    LIVE.cleanupKind = kinds.includes(LIVE.cleanupKind) ? LIVE.cleanupKind : "all";
    const visible = LIVE.cleanupKind === "all"
      ? deletable : deletable.filter((item) => item.type === LIVE.cleanupKind);
    const shown = visible.slice(0, LIVE.cleanupLimit);
    const left = $('[data-page="cleanup"] .panel .card-b');
    if (left) {
      const pendingRows = shown.map((item) =>
        '<div class="clean-row"><span class="k">' + esc(CLEAN_TYPE_LABELS[item.type] || item.type)
        + '</span><div class="t">'
        + esc(item.text || item.id) + '<div class="impact">' + esc(item.id) + '</div></div>'
        + '<span class="r">可删除</span><span class="r"><button type="button" class="link danger" data-live-delete="'
        + esc(item.id) + '" data-type="' + esc(item.type) + '">删除</button></span></div>').join("");
      const runningRows = pendingDeletions.map((row) =>
        '<div class="clean-row"><span class="k">删除</span><div class="t">' + esc(row.target_type)
        + " · " + esc(row.target_id) + '<div class="impact">' + esc(row.reason || "处理中") + '</div></div>'
        + '<span class="r">' + esc(relTime(row.created_at)) + '</span><span class="r">' + esc(row.status)
        + (SERVICE ? '<button class="link" data-live-cleanup-retry="' + esc(row.deletion_id) + '">重试清理</button>' : '') + "</span></div>").join("");
      const chips = kinds.length > 1
        ? '<div class="chips clean-chips">' + [["all", "全部 " + deletable.length]]
          .concat(kinds.map((kind) => [kind, (CLEAN_TYPE_LABELS[kind] || kind) + " "
            + deletable.filter((item) => item.type === kind).length]))
          .map(([key, label]) => '<button type="button" class="chip'
            + (LIVE.cleanupKind === key ? " on" : "") + '" data-clean-kind="' + esc(key) + '">'
            + esc(label) + "</button>").join("") + "</div>"
        : "";
      const more = visible.length > shown.length
        ? '<div class="clean-more"><button type="button" class="btn" data-clean-more>再看 '
          + Math.min(LIVE.cleanupLimit, visible.length - shown.length) + ' 条 · 还有 '
          + (visible.length - shown.length) + ' 条</button></div>'
        : "";
      left.innerHTML = chips + '<div class="clean-group"><h3>可清理 <span class="cnt">'
        + (deletable.length + pendingDeletions.length)
        + '</span></h3>' + (pendingRows + runningRows || '<div class="empty">没有待处理的清理项</div>')
        + "</div>" + more;
    }
    const right = $('[data-page="cleanup"] .panel:nth-child(2) .card-b');
    if (right) {
      const doneDeletions = deletions.filter((row) => row.status === "completed").map((row) =>
        '<div class="clean-row dim" style="grid-template-columns:50px minmax(0,1fr) 86px"><span class="k">删除</span>'
        + '<div class="t">' + esc(row.target_type) + " · " + esc(row.target_id) + '</div><span class="r">'
        + esc(relTime(row.completed_at || row.created_at)) + "</span></div>").join("");
      const doneCorrections = corrections.map((row) =>
        '<div class="clean-row dim" style="grid-template-columns:50px minmax(0,1fr) 86px"><span class="k">更正</span>'
        + '<div class="t">' + esc(row.old_text) + " → " + esc(row.new_text) + '</div><span class="r">'
        + esc(row.store_status === "retracted" ? "已撤回" : relTime(row.created_at))
        + (SERVICE && row.store_status === "active" ? '<button class="link" data-live-correction-retract="' + esc(row.id) + '">撤回更正</button>' : '') + "</span></div>").join("");
      right.innerHTML = '<div class="clean-group" style="padding-top:14px">'
        + (doneDeletions + doneCorrections || '<div class="empty">暂无已完成记录</div>') + "</div>";
    }
    const footer = $('[data-page="cleanup"] .panel .card-f');
    if (footer) footer.innerHTML = '<span class="dim">删除屏障会阻止同一来源被重新写入</span><a data-nav="settings" href="#settings">清理设置 →</a>';
  }

  /* ---------- settings ---------- */

  function renderSettings() {
    const engine = LIVE.engine || {};
    const evolution = LIVE.snapshot.evolution || {};
    const sub = $('[data-page="settings"] .page-sub');
    if (sub) sub.textContent = "本机 · 改动走引擎，不直接写存储";
    const tools = $('[data-page="settings"] .page-tools');
    if (tools) tools.innerHTML = (SERVICE ? '<button type="button" class="btn" data-live-maintain>运行一次维护</button>' : '')
      + '<button type="button" class="btn" data-live-export-settings>导出引擎信息</button>';
    const panel = $('[data-page="settings"] .panel');
    if (panel) {
      const groups = panel.querySelectorAll('.group');
      if (groups[0]) {
        const modeOff = evolution.mode === "off";
        groups[0].innerHTML = '<h3>演化</h3>'
          + '<div class="switch"><div><div class="t">后台自动演化</div><div class="d">'
          + (modeOff ? "关闭时不会评估新提案" : "按冷却时间后台评估提案") + '</div></div>'
          + '<button type="button" class="sw' + (evolution.enabled ? " on" : "") + '" role="switch" aria-checked="'
          + (evolution.enabled ? "true" : "false") + '" data-live-evolution="' + (evolution.enabled ? "0" : "1") + '"></button></div>'
          + '<div class="switch"><div><div class="t">演化模式</div><div class="d">关闭 / 提议 / 自动</div></div>'
          + '<div class="chips" style="margin-left:auto">' + ["off", "propose", "auto"].map((mode) =>
            '<button type="button" class="chip' + (evolution.mode === mode ? " on" : "") + '" data-live-mode="' + mode + '">'
            + ({ off: "关闭", propose: "提议", auto: "自动" }[mode]) + "</button>").join("") + "</div></div>"
          + (groups[0].querySelector(".group-note") ? "" : '<p class="group-note dim" style="font-size:12.5px;color:var(--ink-3);margin:10px 20px 4px">关闭时不会生成新提案；提议只生成待审核项，自动会直接采用可演化段。</p>');
      }
      const memGroup = $("#memBehaviorGroup");
      if (memGroup) memGroup.remove();
    }
    const info = $("#sysInfo");
    if (info) {
      const rows = [
        ["数据根", engine.root || "—", true],
        ["角色", engine.character || "—", false],
        ["人格修订", "第 " + (engine.card_revision || 0) + " 版", false],
        ["发布版本", engine.published_version ? String(engine.published_version).slice(0, 8) : "—", true],
        ["演化模式", engine.evolution_mode || "off", false],
        ["写入", engine.read_only ? "只读" : "允许", false],
      ];
      if (SERVICE) {
        const chat = (engine.capabilities || {}).chat || {};
        rows.push(["对话", chat.status === "ready" ? "已就绪" : "待配置模型或发布人格", false]);
        (engine.config_missing || []).forEach((field) => rows.push(["配置待填写", field, true]));
      }
      info.innerHTML = rows.map(([key, value, mono]) =>
        '<div class="info-row"><span class="k">' + key + '</span><span class="v' + (mono ? " mono" : "") + '">'
        + esc(value) + "</span></div>").join("");
    }
    const sysHint = $("#sysHint");
    if (sysHint) sysHint.textContent = engine.read_only ? "只读" : "可写";
    const about = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .card-b .reason');
    if (about) about.textContent = "铃芽 RIN · 记忆管理台。数据留在本机，改动经引擎写入。";
    const aboutAct = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .detail-act');
    if (aboutAct) aboutAct.remove();
    const aboutFooter = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .card-f');
    if (aboutFooter) aboutFooter.innerHTML = '<span class="dim">改动走引擎，不直接写存储</span>';
    renderModelConfig();
  }

  /* ---------- model configuration ---------- */

  const MODEL_CFG = { data: null, loaded: false, busy: false, clearKey: false };

  function modelConfigApply(data) {
    MODEL_CFG.data = data;
    MODEL_CFG.loaded = true;
    if (!data || !data.available) {
      const hint = $("#modelCfgHint");
      if (hint) hint.textContent = (data && data.reason) || "不可用";
      const form = $("#modelCfgForm");
      if (form) form.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
      return;
    }
    const set = (sel, value) => { const node = $(sel); if (node) node.value = value == null ? "" : value; };
    set("#cfgBaseUrl", data.base_url || "");
    set("#cfgModelConv", (data.models || {}).conversation || "");
    set("#cfgModelSummary", (data.models || {}).summary || "");
    const effort = $("#cfgEffort");
    if (effort && !effort.options.length) {
      effort.innerHTML = (data.efforts || []).map((item) =>
        '<option value="' + esc(item.value) + '">' + esc(item.label) + "</option>").join("");
    }
    set("#cfgEffort", data.reasoning_effort || "");
    const key = $("#cfgApiKey");
    if (key) key.value = "";
    MODEL_CFG.clearKey = false;

    const keyState = $("#cfgKeyState");
    const info = data.api_key || {};
    if (keyState) {
      if (MODEL_CFG.clearKey) keyState.textContent = "保存后将清除已保存的 Key";
      else if (info.stored) keyState.textContent = "已保存（由管理台录入）";
      else if (info.source === "environment") keyState.textContent = "来自环境变量 " + (info.env_name || "");
      else keyState.textContent = "尚未保存";
    }
    const clear = $("#cfgKeyClear");
    if (clear) clear.hidden = !info.stored;

    const hint = $("#modelCfgHint");
    if (hint) {
      hint.textContent = data.demo ? "离线演示"
        : (data.configured ? (info.present ? "已配置" : "缺少 Key") : "未配置");
    }
    const file = $("#cfgFile");
    if (file) file.textContent = data.demo ? "" : (data.file || "");
  }

  async function loadModelConfig(force) {
    if (MODEL_CFG.loaded && !force) return;
    try {
      modelConfigApply(await api("/api/model/config"));
    } catch (error) {
      const hint = $("#modelCfgHint");
      if (hint) hint.textContent = "读取失败：" + error.message;
    }
  }

  function renderModelConfig() {
    if (!LIVE.connected) return;
    if (!MODEL_CFG.loaded) loadModelConfig(true);
  }

  function modelConfigResult(message, kind) {
    const node = $("#cfgResult");
    if (!node) return;
    node.textContent = message;
    node.className = "result" + (kind ? " " + kind : "");
  }

  async function saveModelConfig() {
    if (MODEL_CFG.busy) return;
    const body = {
      base_url: ($("#cfgBaseUrl") || {}).value || "",
      models: {
        conversation: (($("#cfgModelConv") || {}).value || "").trim(),
        summary: (($("#cfgModelSummary") || {}).value || "").trim(),
      },
      reasoning_effort: ($("#cfgEffort") || {}).value || "",
    };
    const keyInput = $("#cfgApiKey");
    if (keyInput && keyInput.value) body.api_key = keyInput.value;
    if (MODEL_CFG.clearKey) body.clear_api_key = true;

    const button = $("#cfgSave");
    MODEL_CFG.busy = true;
    if (button) { button.disabled = true; button.textContent = "保存中…"; }
    modelConfigResult("");
    try {
      const data = await api("/api/model/config", { method: "POST", body: JSON.stringify(body) });
      modelConfigApply(data);
      modelConfigResult("已保存并应用", "ok");
      toast("模型配置已生效");
      if (CHAT) CHAT.loaded = false;
      await refresh();
    } catch (error) {
      // A rejected value keeps the form as typed so it can be corrected.
      modelConfigResult("保存失败：" + error.message, "bad");
    } finally {
      MODEL_CFG.busy = false;
      if (button) { button.disabled = false; button.textContent = "保存并应用"; }
    }
  }

  const MODEL_FAIL_HINTS = {
    auth: "服务商拒绝了这个 Key",
    config: "模型名或接口地址不对",
    timeout: "连接超时",
    rate_limit: "服务商限流，稍后重试",
    transport: "连不上接口地址",
    format: "返回内容无法解析",
  };

  async function testModelConfig() {
    if (MODEL_CFG.busy) return;
    const button = $("#cfgTest");
    MODEL_CFG.busy = true;
    if (button) { button.disabled = true; button.textContent = "测试中…"; }
    modelConfigResult("正在连接模型…");
    try {
      const result = await api("/api/model/test", { method: "POST", body: "{}" });
      if (result.ok) {
        modelConfigResult("连接成功 · " + (result.reply || "OK"), "ok");
      } else {
        const hint = MODEL_FAIL_HINTS[result.kind] || "调用失败";
        modelConfigResult(hint + "（" + result.kind + "）", "bad");
      }
    } catch (error) {
      modelConfigResult("测试失败：" + error.message, "bad");
    } finally {
      MODEL_CFG.busy = false;
      if (button) { button.disabled = false; button.textContent = "测试连接"; }
    }
  }

  function bindModelConfig() {
    const save = $("#cfgSave");
    if (save) save.addEventListener("click", saveModelConfig);
    const test = $("#cfgTest");
    if (test) test.addEventListener("click", testModelConfig);
    const clear = $("#cfgKeyClear");
    if (clear) clear.addEventListener("click", () => {
      MODEL_CFG.clearKey = true;
      const key = $("#cfgApiKey");
      if (key) key.value = "";
      modelConfigApply(MODEL_CFG.data);
      modelConfigResult("保存后才会清除", "");
    });
    const form = $("#modelCfgForm");
    if (form) form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveModelConfig();
    });
  }

  /* ---------- conversation ---------- */

  const CHAT = { messages: [], status: null, turns: [], sending: false, loaded: false };

  function chatTime(iso) {
    const text = String(iso || "");
    if (!text) return "";
    return " · " + relTime(text);
  }

  function chatBubble(row) {
    const who = row.role === "assistant" ? "铃" : "你";
    const node = document.createElement("div");
    node.className = "chat-row " + (row.role === "assistant" ? "ai" : "user");
    const badge = document.createElement("span");
    badge.className = "chat-who";
    badge.textContent = who;
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = row.text;
    if (row.meta) {
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = row.meta;
      bubble.appendChild(meta);
    }
    const message = document.createElement("div");
    message.className = "chat-message";
    message.appendChild(bubble);
    if (row.id) {
      const actions = document.createElement("div");
      actions.className = "chat-message-actions";
      const addAction = (label, action, requiresModel) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.dataset.chatMutate = "1";
        if (requiresModel) button.dataset.requiresModel = "1";
        button.addEventListener("click", action);
        actions.appendChild(button);
      };
      addAction("编辑", () => editChatMessage(row));
      if (row.role === "assistant") addAction("重新生成", () => regenerateChat(row), true);
      message.appendChild(actions);
    }
    if (row.role === "assistant" && !row.pending) {
      const thoughts = document.createElement("details");
      thoughts.className = "chat-thoughts";
      const summary = document.createElement("summary");
      summary.textContent = "查看思考内容";
      const content = document.createElement("pre");
      content.textContent = row.reasoning || "这条消息没有保存模型返回的思考内容。";
      thoughts.append(summary, content);
      message.appendChild(thoughts);
    }
    node.appendChild(badge);
    node.appendChild(message);
    return node;
  }

  function chatLogNode() {
    const log = $("#chatLog");
    return log;
  }

  function chatPinned() {
    const log = chatLogNode();
    if (!log) return true;
    return log.scrollHeight - log.scrollTop - log.clientHeight < 90;
  }

  function scrollChat(force) {
    const log = chatLogNode();
    if (log && (force || chatPinned())) log.scrollTop = log.scrollHeight;
  }

  function chatAppend(row) {
    const log = chatLogNode();
    if (!log) return;
    const empty = $("#chatEmpty");
    if (empty) empty.remove();
    const stick = chatPinned();
    const node = chatBubble(row);
    node.dataset.chatPending = row.pending ? "1" : "0";
    log.appendChild(node);
    if (stick) scrollChat(true);
    return node;
  }

  function renderChatMessages() {
    const log = chatLogNode();
    if (!log) return;
    log.innerHTML = "";
    if (!CHAT.messages.length) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      empty.id = "chatEmpty";
      empty.innerHTML = "<b>开始一段对话</b>说点什么，这一轮会用已发布的人格回答，<br>并把双方原文写进记忆。";
      log.appendChild(empty);
      return;
    }
    CHAT.messages.forEach((row) => log.appendChild(chatBubble(row)));
    scrollChat(true);
  }

  function renderChatStatus() {
    const state = CHAT.status;
    const stateNode = $("#chatState");
    const stateText = $("#chatStateText");
    const input = $("#chatInput");
    const send = $("#chatSend");
    const hint = $("#chatHint");
    const capHint = $("#chatCapHint");
    const ready = Boolean(state && state.ready);
    if (stateNode) stateNode.classList.toggle("warn", !ready);
    if (stateText) {
      if (!state) stateText.textContent = "对话服务不可用";
      else if (ready) stateText.textContent = CHAT.sending ? "正在生成这一轮…" : "就绪 · 消息会写入记忆";
      else stateText.textContent = state.reason || "对话不可用";
    }
    if (input) input.disabled = !ready || CHAT.sending;
    if (send) send.disabled = !ready || CHAT.sending;
    const writable = Boolean(state && state.writable !== false);
    $$("[data-chat-mutate]").forEach((button) => {
      button.disabled = CHAT.sending || !writable || (button.dataset.requiresModel === "1" && !ready);
    });
    const clear = $("#chatClear");
    if (clear) clear.disabled = CHAT.sending || !writable || !CHAT.messages.length;
    const reload = $("#chatReload");
    if (reload) reload.disabled = CHAT.sending;
    if (hint) hint.textContent = CHAT.messages.length ? CHAT.messages.length + " 条消息" : "尚未开始";
    if (capHint) capHint.textContent = ready ? "就绪" : "不可用";

    const who = $("#chatWho");
    if (who) who.textContent = (state && (state.display_name || state.character)) || "—";
    const persona = $("#chatPersona");
    if (persona) persona.textContent = (state && state.persona_version) || "尚未发布";
    const revision = $("#chatRevision");
    if (revision) revision.textContent = state && state.card_revision ? "第 " + state.card_revision + " 版" : "—";
    const model = $("#chatModel");
    if (model) {
      const info = (state && state.model) || {};
      model.textContent = info.demo ? "离线演示模型" : (info.configured ? (info.name || info.tier || "conversation") : "未配置");
    }
    const storage = $("#chatStorage");
    if (storage) {
      const caps = (state && state.capabilities) || {};
      const item = caps.storage || {};
      storage.textContent = item.status === "ready" ? "可写" : (item.reason || item.status || "不可用");
    }
    renderChatTurns();
  }

  function renderChatTurns() {
    const body = $("#chatTurns");
    const hint = $("#chatTurnHint");
    if (!body) return;
    if (!CHAT.turns.length) {
      body.innerHTML = '<p class="dim" style="margin:0;font-size:13.5px">还没有提交记录。</p>';
      if (hint) hint.textContent = "—";
      return;
    }
    if (hint) hint.textContent = CHAT.turns.length + " 轮";
    body.innerHTML = CHAT.turns.slice(0, 12).map((turn) => {
      const status = String(turn.status || "");
      const label = status === "committed" ? (turn.delivered ? "已显示 · 已提交" : "已提交")
        : status === "deleted" ? "已失效"
        : status === "failed" ? "失败"
        : status === "pending" ? "处理中"
        : status === "interrupted" ? "中断" : status;
      const cls = status === "committed" ? "ok" : (status === "deleted" ? "warn" : "wait");
      return '<div class="chat-turn"><span>' + esc(relTime(turn.occurred_at)) + '</span>'
        + '<span class="st ' + cls + '">' + esc(label) + "</span></div>";
    }).join("");
  }

  async function loadChat(force) {
    if (CHAT.loaded && !force) return;
    try {
      const history = await api("/api/chat/history?limit=200");
      CHAT.messages = (history.messages || []).map((row) => ({
        id: row.id, role: row.role === "assistant" ? "assistant" : "user", text: row.text, reasoning: row.reasoning || "",
      }));
      CHAT.turns = history.turns || [];
      CHAT.status = history.status || null;
      CHAT.loaded = true;
      renderChatMessages();
      renderChatStatus();
    } catch (error) {
      CHAT.status = { ready: false, reason: "读取对话失败：" + error.message };
      renderChatStatus();
    }
  }

  function renderChat() {
    if (!LIVE.connected) return;
    if (!CHAT.loaded) {
      loadChat(true);
      return;
    }
    renderChatStatus();
  }

  const pendingKey = "rin-chat-pending:" + location.origin;
  let pendingRequest = null;
  try { pendingRequest = JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch (_) { /* invalid local draft */ }

  async function changeChat(action, payload) {
    if (CHAT.sending) return;
    CHAT.sending = true;
    renderChatStatus();
    let pending;
    if (action === "regenerate") pending = chatAppend({ role: "assistant", text: "正在重新想…", pending: true });
    try {
      const result = await api("/api/chat/" + action, { method: "POST", body: JSON.stringify(payload || {}) });
      if (action === "clear") {
        pendingRequest = null;
        sessionStorage.removeItem(pendingKey);
      }
      if (action === "regenerate" && SERVICE && result.status === "committed") {
        await api("/v1/receipts", { method: "POST", body: JSON.stringify({
          request_id: result.request_id, receipt_id: "display:" + result.request_id,
          kind: "delivered", session_id: "console", scene: "", delivered_chars: Array.from(result.reply || "").length,
        }) });
      }
      await loadChat(true);
      toast(action === "clear" ? "对话已清空" : action === "edit" ? "消息已更新" : "已重新生成");
    } catch (error) {
      toast("操作未完成：" + error.message);
      await loadChat(true);
    } finally {
      if (pending) pending.remove();
      CHAT.sending = false;
      renderChatStatus();
    }
  }

  function editChatMessage(row) {
    openLiveModal("编辑历史消息",
      '<p class="dim">更新聊天文字，之后的对话会使用编辑后的内容。</p>'
      + '<textarea class="chat-edit-input" id="chatEditText" maxlength="8000" aria-label="编辑消息">' + esc(row.text) + '</textarea>',
      "保存修改", async () => {
        const text = ($("#chatEditText").value || "").trim();
        if (!text) { toast("消息不能为空"); return; }
        await changeChat("edit", { message_id: row.id, text });
      });
    const input = $("#chatEditText");
    if (input) input.focus();
  }

  function regenerateChat(row) {
    const run = () => changeChat("regenerate", { message_id: row.id, request_id: rid("regenerate") });
    const index = CHAT.messages.indexOf(row);
    if (index < CHAT.messages.length - 1) {
      openLiveModal("从这里重新生成", "<p>这条回复及其后的聊天会被新回复替换，继续吗？</p>", "重新生成", run);
    } else run();
  }

  async function sendChat() {
    if (CHAT.sending) return;
    const input = $("#chatInput");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    if (!pendingRequest || pendingRequest.text !== text) pendingRequest = { text, request_id: rid("chat") };
    sessionStorage.setItem(pendingKey, JSON.stringify(pendingRequest));
    CHAT.sending = true;
    input.value = "";
    input.style.height = "";
    chatAppend({ role: "user", text });
    const pending = chatAppend({ role: "assistant", text: "正在想…", pending: true });
    renderChatStatus();
    try {
      const result = await api("/api/chat/send", { method: "POST", body: JSON.stringify(pendingRequest) });
      if (result.status !== "committed") throw new Error(result.message || "轮次仍在处理中，请重试同步");
      const reply = String(result.reply || "");
      if (pending) pending.remove();
      chatAppend({ role: "assistant", text: reply, meta: result.replayed ? "已回放同一轮提交" : "" });
      if (SERVICE) await api("/v1/receipts", { method: "POST", body: JSON.stringify({
        request_id: result.request_id, receipt_id: "display:" + result.request_id,
        kind: "delivered", session_id: "console", scene: "", delivered_chars: Array.from(reply).length,
      }) });
      pendingRequest = null;
      sessionStorage.removeItem(pendingKey);
      await loadChat(true);
      await refresh();
    } catch (error) {
      if (pending) pending.remove();
      chatAppend({ role: "assistant", text: "同步未完成：" + error.message, meta: "重试会沿用同一请求" });
      toast("同步未完成，可重试同一轮");
      input.value = text;
    } finally {
      CHAT.sending = false;
      renderChatStatus();
      input.focus();
    }
  }

  function bindChat() {
    if (pendingRequest && $("#chatInput")) $("#chatInput").value = pendingRequest.text;
    const form = $("#chatForm");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        sendChat();
      });
    }
    const input = $("#chatInput");
    if (input) {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          sendChat();
        }
      });
      input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = Math.min(180, input.scrollHeight) + "px";
      });
    }
    const reload = $("#chatReload");
    if (reload) reload.addEventListener("click", () => { loadChat(true); toast("已刷新对话"); });
    const clear = $("#chatClear");
    if (clear) clear.addEventListener("click", () => {
      openLiveModal("清空对话", "<p>清空当前网页的聊天记录，已记住的事实和人格保留。</p>", "清空对话", () => changeChat("clear"));
    });
  }

  /* ---------- render all ---------- */

  function renderAll() {
    renderChrome();
    renderOverview();
    renderChat();
    renderTimeline();
    renderFacts();
    renderReflections();
    renderPersona();
    renderPublish();
    renderEvolution();
    renderCleanup();
    renderSettings();
  }

  async function refresh() {
    try {
      let initialization = null;
      if (SERVICE) {
        writeIdentity = await api("/v1/bootstrap");
        initialization = writeIdentity.initialization;
      }
      const [snapshot, engine] = await Promise.all([
        api("/api/snapshot"), api("/api/engine/status"),
      ]);
      LIVE.snapshot = snapshot;
      LIVE.engine = engine;
      LIVE.connected = true;
      document.body.classList.add("rin-live");
      renderAll();
      if (SERVICE) {
        $$('a[href="/api/export"]').forEach((link) => link.href = "/v1/export");
        renderInitialization(initialization);
      }
      return true;
    } catch (error) {
      LIVE.connected = false;
      document.body.classList.remove("rin-live");
      const navMode = $(".nav-mode");
      if (navMode) navMode.innerHTML = SERVICE ? "<i></i>服务已断开" : "<i></i>离线预览";
      const footerStatus = $(".foot-right span");
      if (footerStatus) footerStatus.textContent = "离线预览 · 启动本地服务后自动连接";
      if (SERVICE) renderInitialization(null, error.message);
      return false;
    }
  }

  function renderInitialization(state, failure) {
    let panel = $("#rinSetup");
    if (state && state.published && !failure) {
      if (panel) panel.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "rinSetup";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-labelledby", "rinSetupTitle");
      panel.style.cssText = "position:fixed;inset:0;z-index:10000;display:grid;place-items:center;"
        + "background:rgba(243,239,229,.96);padding:32px";
      document.body.appendChild(panel);
    }
    const initialized = state && state.initialized;
    panel.innerHTML = '<form id="rinSetupForm" class="panel" style="width:min(580px,90vw);padding:32px">'
      + '<div class="page-en">铃芽 RIN</div><h2 id="rinSetupTitle">'
      + (failure ? "记忆服务已断开" : initialized ? "发布初始人格" : "创建你的新伙伴") + '</h2>'
      + (failure ? '<p role="alert">' + esc(failure) + '</p><button type="button" class="btn pri" id="rinReconnect">重新连接</button>'
        : '<p>' + (initialized ? "人格已保存。发布后，后续对话将使用这个版本。" : "为新的记忆空间填写名字与初始人格。") + '</p>'
        + '<p class="dim">角色标识 · ' + esc(state.character_id) + '</p>'
        + '<label for="rinDisplayName">显示名</label><input id="rinDisplayName" required maxlength="80" value="'
        + esc(state.display_name) + '" style="display:block;width:100%;margin:8px 0 20px;padding:10px">'
        + (initialized ? ''
          : '<label for="rinInitialPersona">初始人格</label><textarea id="rinInitialPersona" required maxlength="4000" rows="7"'
          + ' style="display:block;width:100%;margin:8px 0 20px;padding:10px;font:inherit" placeholder="她是谁、如何说话、与你如何相处……"></textarea>')
        + '<p id="rinSetupError" role="alert"></p><button class="btn pri" id="rinSetupSubmit" type="submit">'
        + (initialized ? "发布并进入管理台" : "保存初始人格") + '</button>')
      + '</form>';
    const reconnect = $("#rinReconnect");
    if (reconnect) reconnect.addEventListener("click", refresh);
    $("#rinSetupForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#rinSetupSubmit");
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = "正在保存…";
      try {
        if (initialized) {
          // The name is the first line of the identity section, so renaming is
          // one card revision like any other edit; publish binds to the
          // revision this form now has.
          let revision = state.revision;
          const name = ($("#rinDisplayName").value || "").trim();
          if (name && name !== state.display_name) {
            const card = await api("/v1/persona");
            const identity = ((card && card.sections) || []).find((row) => row.section_id === "identity");
            if (identity) {
              const receipt = await api("/v1/persona/revise", {
                method: "POST",
                body: JSON.stringify({
                  base_revision: Number(card.revision) || 0,
                  operations: [{ op: "replace", section_id: "identity",
                                 text: withNameLine(identity.text, name) }],
                  reason: "console_rename",
                }),
              });
              revision = Number(receipt.revision) || revision;
            }
          }
          await api("/v1/persona/publish", { method: "POST", body: JSON.stringify({ revision, target: "next_turn" }) });
        } else {
          await api("/v1/characters", { method: "POST", body: JSON.stringify({
            character_id: state.character_id, display_name: $("#rinDisplayName").value,
            text: $("#rinInitialPersona").value,
          }) });
        }
        CHAT.loaded = false;
        await refresh();
      } catch (error) {
        $("#rinSetupError").textContent = error.message;
        button.disabled = false;
        button.textContent = "重试保存";
      }
    });
  }

  /* ---------- modal / drawer ---------- */

  function openLiveDrawer(title, sub, html) {
    const drawer = $("#drawer");
    if (!drawer) return;
    $("#drawerTitle").textContent = title;
    $("#drawerSub").textContent = sub;
    $("#drawerBody").innerHTML = html;
    drawer.classList.add("on");
    $("#drawerMask").classList.add("on");
    drawer.setAttribute("aria-hidden", "false");
    const x = $(".drawer-x", drawer);
    if (x) x.focus();
  }

  function openLiveModal(title, body, confirmLabel, onConfirm) {
    const modal = $("#modal");
    if (!modal) return;
    LIVE.modalConfirm = onConfirm || null;
    $("#modalBody").innerHTML = "<h3>" + esc(title) + "</h3>" + body
      + '<div class="act"><button type="button" class="btn" data-live-modal-cancel>取消</button>'
      + '<button type="button" class="btn pri" data-live-modal-confirm>' + esc(confirmLabel) + "</button></div>";
    modal.classList.add("on");
    const x = $("#modalX");
    if (x) x.focus();
  }

  function closeLiveModal() {
    const modal = $("#modal");
    if (modal) modal.classList.remove("on");
    LIVE.modalConfirm = null;
  }

  /* ---------- live actions ---------- */

  function openPendingDrawer() {
    const proposals = (LIVE.proposals || []).filter((row) => row.status === "proposed");
    if (!proposals.length) {
      openLiveDrawer("全部待处理", "没有待处理的提案", '<div class="empty">记忆在安静地生长</div>');
      return;
    }
    openLiveDrawer("全部待处理", proposals.length + " 项提案待审核", proposals.map((proposal) => {
      const diff = proposalDiff(proposal);
      return '<article class="dec" data-live-key="' + esc(proposal.proposal_id) + '"><div class="dec-t">'
        + esc(proposalTitle(proposal)) + '</div><div class="diff"><span class="lab">修改前</span><p><del>'
        + esc(diff.before) + '</del></p><span class="lab">改为</span><p><ins>' + esc(diff.after)
        + '</ins></p></div><div class="dec-src">来源 · ' + esc(proposal.reason || "演化提案") + " · "
        + relTime(proposal.created_at) + '</div><div class="dec-act"><button type="button" class="btn pri" data-live-prop-accept="'
        + esc(proposal.proposal_id) + '">采纳</button><button type="button" class="btn" data-live-prop-reject="'
        + esc(proposal.proposal_id) + '">驳回</button></div></article>';
    }).join(""));
  }

  function openPublishDrawer() {
    const persona = LIVE.snapshot.persona || {};
    const current = (persona.versions && persona.versions.current) || {};
    const consumers = persona.consumers || {};
    openLiveDrawer("发布详情", "第 " + (current.revision || 0) + " 版 · " + (current.version || "—"),
      '<div class="consumers" style="grid-template-columns:1fr 1fr;margin-top:0">'
      + Object.keys(consumers).map((name) => {
        const row = consumers[name] || {};
        const disconnected = row.status === "not_connected";
        return '<div class="consumer' + (disconnected ? " offline" : row.version === current.version ? "" : " wait") + '"><div class="n"><i></i>'
          + esc(CONSUMER_LABELS[name] || name) + '</div><div class="st">'
          + (disconnected ? "未接入" : row.version === current.version ? "已采用" : "待同步") + '</div><div class="t">'
          + (disconnected ? "接入后报告版本采用状态" : row.version ? esc(String(row.version).slice(0, 8)) : "尚未采用") + "</div></div>";
      }).join("") + "</div>"
      + '<div style="margin-top:18px"><div class="token-row"><span>版本令牌</span><b title="'
      + esc(current.version || "") + '">' + esc(current.version ? String(current.version).slice(0, 8) : "—")
      + '</b></div><div class="token-row"><span>发布于</span><b>' + esc(current.published_at ? relTime(current.published_at) : "—")
      + '</b></div></div><p class="note">只统计已接入的使用方；回滚保留切换后新增的对话、更正与删除记录。</p>'
      + '<div class="detail-act"><button type="button" class="btn" data-live-refresh>刷新状态</button>'
      + '<button type="button" class="btn pri" data-live-publish="next_turn">发布到下一轮</button></div>');
  }

  // The name lives on the identity section's first line, written as
  // `名字: <name>`. These helpers read and rewrite that line so the editor can
  // offer a plain name box while the stored text keeps one obvious shape.
  function nameLineOf(text) {
    const line = String(text || "").split("\n").map((row) => row.trim()).find((row) => row.length);
    if (!line) return "";
    const match = line.match(/^(名字|姓名)[:：]\s*(.*)$/);
    return match ? match[2].trim() : "";
  }

  function withNameLine(text, name) {
    const lines = String(text || "").split("\n");
    const first = (lines.find((row) => row.trim()) || "").trim();
    const rest = /^(名字|姓名)[:：]/.test(first) ? lines.slice(lines.indexOf(lines.find((row) => row.trim())) + 1) : lines;
    return ["名字: " + name].concat(rest).join("\n").replace(/\n{3,}/g, "\n\n");
  }

  function openPersonaEditor() {
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const rows = (card.sections || []).filter((section) => section.editable_by && section.editable_by.user);
    const fieldStyle = 'width:100%;padding:10px 12px;border:1px solid var(--line);'
      + 'background:rgba(255,253,248,.9);font:inherit;font-size:14.5px;line-height:1.7;color:var(--ink)';
    const name = nameLineOf((rows.find((row) => row.section_id === "identity") || {}).text);
    openLiveDrawer("编辑人格卡", "基于第 " + (card.revision || 0) + " 版 · 保存会创建新版本",
      '<div class="detail-sec" style="margin-top:0"><h4>名字</h4>'
      + '<input id="liveNameField" maxlength="80" value="' + esc(name) + '" style="' + fieldStyle + '">'
      + '<p class="dim" style="font-size:12.5px;margin:6px 0 0">保存在身份段第一行「名字: …」，下面正文无需重复写名字。</p></div>'
      + rows.map((section) => '<div class="detail-sec" style="margin-top:0"><h4>' + esc(sectionLabel(section.section_id))
        + '</h4><textarea data-live-edit-text="' + esc(section.section_id) + '" rows="6" style="'
        + fieldStyle + ';resize:vertical">' + esc(section.text) + "</textarea></div>").join("")
      + '<details style="margin:16px 0"><summary>新增可演化段落</summary><label>段落类型 <select id="newSectionKind">'
      + ["tone", "relationship", "habits", "interests", "boundaries"].filter((kind) => !(card.sections || []).some((row) => row.section_id === kind))
        .map((kind) => '<option value="' + kind + '">' + esc(sectionLabel(kind)) + '</option>').join("")
      + '</select></label><textarea id="newSectionText" rows="3" style="display:block;width:100%;margin-top:10px;font:inherit" placeholder="填写段落内容，留空则不新增"></textarea></details>'
      + '<div class="detail-act"><button type="button" class="btn" data-live-editor-cancel>取消</button>'
      + '<button type="button" class="btn pri" data-live-editor-save>保存为新版本</button></div>');
    $("#drawer").dataset.baseRevision = String(card.revision);
    $("#drawer").dataset.nameLine = name;
  }

  async function savePersonaEditor() {
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const operations = [];
    const nameField = $("#liveNameField");
    const name = nameField ? nameField.value.trim() : "";
    const identity = (card.sections || []).find((row) => row.section_id === "identity");
    const identityField = $('[data-live-edit-text="identity"]');
    // The name box and the identity body are one stored section: write the
    // name line back into the body the user is editing, then revise once.
    if (identity && (name !== ($("#drawer").dataset.nameLine || "") || identityField)) {
      const body = identityField ? identityField.value : identity.text;
      const merged = name ? withNameLine(body, name) : body;
      if (merged !== identity.text) {
        operations.push({ op: "replace", section_id: "identity", text: merged });
      }
    }
    $$("[data-live-edit-text]").forEach((node) => {
      if (node.dataset.liveEditText === "identity") return;
      const section = (card.sections || []).find((row) => row.section_id === node.dataset.liveEditText);
      if (section && node.value !== section.text) {
        operations.push({ op: "replace", section_id: section.section_id, text: node.value });
      }
    });
    const newText = $("#newSectionText");
    if (newText && newText.value.trim()) {
      const kind = $("#newSectionKind").value;
      if (!kind) { toast("所有段落类型均已存在，请编辑已有段落"); return; }
      operations.push({ op: "add", section_id: kind, kind, text: newText.value.trim(),
                        editable_by: { user: true, evolution: true }, scope: { subject: (LIVE.snapshot.meta || {}).subject || "private" } });
    }
    if (!operations.length) { toast("没有修改"); return; }
    const result = await write("/api/persona/revise", {
      base_revision: Number($("#drawer").dataset.baseRevision),
      operations,
      reason: "webui edit",
      request_id: rid("ui-revise"),
    }, (result) => decisionToast(result, "已保存为新版本"));
    if (result) {
      const drawer = $("#drawer");
      if (drawer) drawer.classList.remove("on");
      const mask = $("#drawerMask");
      if (mask) mask.classList.remove("on");
    }
  }

  function openDeleteEvent() {
    const event = LIVE.selectedEvent;
    if (!event) { toast("请先选择一条记忆"); return; }
    openLiveModal("删除这条记忆？",
      "<p>删除后同一来源的事件不会因重放被重新写入；派生的事实与反思会一并失效。</p>"
      + '<p class="meta">记忆编号 · ' + esc(event.id) + "　|　会生成墓碑，仅保留来源指纹</p>",
      "删除", async () => {
        const result = await write("/api/cleanup/delete", {
          source_key: event.source_key, reason: "webui delete", request_id: rid("ui-delete"),
        }, "已删除 · 已生成墓碑");
        if (result) {
          const drawer = $("#drawer");
          if (drawer) drawer.classList.remove("on");
        }
      });
  }

  function openCorrectionModal() {
    const event = LIVE.selectedEvent;
    if (!event) { toast("请先选择一条记忆"); return; }
    openLiveModal("更正这条记忆",
      "<p>填写更正后的文本。引擎会生成更正请求，并失效旧事实与派生内容。</p>"
      + '<p class="meta">原文 · ' + esc(event.text) + "</p>"
      + '<label style="display:block;margin-top:12px"><span style="font-size:13px;color:var(--ink-3)">更正为</span>'
      + '<textarea id="liveCorrectText" rows="3" style="width:100%;margin-top:6px;padding:10px 12px;border:1px solid var(--line);'
      + 'background:rgba(255,253,248,.9);font:inherit;font-size:14.5px;line-height:1.7;color:var(--ink);resize:vertical">'
      + esc(event.text) + "</textarea></label>",
      "提交更正", async () => {
        const value = ($("#liveCorrectText") || {}).value || "";
        if (!value.trim()) { toast("更正内容不能为空"); return; }
        await write("/api/cleanup/correct", {
          old_text: event.text, new_text: value.trim(), request_id: rid("ui-correct"),
        }, "更正已提交");
      });
  }

  /* ---------- delegation ---------- */

  document.addEventListener("click", async (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const accept = target.closest("[data-live-prop-accept]");
    if (accept) {
      event.preventDefault();
      const id = accept.dataset.livePropAccept;
      accept.disabled = true;
      accept.textContent = "处理中…";
      await write("/api/evolution/" + encodeURIComponent(id) + "/accept", { reason: "webui accept" },
        (result) => decisionToast(result, "提案已采纳"));
      return;
    }
    const reject = target.closest("[data-live-prop-reject]");
    if (reject) {
      event.preventDefault();
      const id = reject.dataset.livePropReject;
      reject.disabled = true;
      reject.textContent = "处理中…";
      await write("/api/evolution/" + encodeURIComponent(id) + "/reject", { reason: "webui reject" },
        (result) => decisionToast(result, "提案已驳回"));
      return;
    }
    const withdraw = target.closest("[data-live-prop-withdraw]");
    if (withdraw) {
      event.preventDefault();
      const id = withdraw.dataset.livePropWithdraw;
      withdraw.disabled = true;
      await write("/api/evolution/" + encodeURIComponent(id) + "/withdraw", { reason: "webui withdraw" },
        (result) => decisionToast(result, "提案已撤回"));
      return;
    }
    const mode = target.closest("[data-live-mode]");
    if (mode) {
      event.preventDefault();
      const next = mode.dataset.liveMode;
      await write("/api/evolution/mode", { mode: next }, "演化模式已更新");
      const enabled = !!(LIVE.snapshot && LIVE.snapshot.evolution && LIVE.snapshot.evolution.enabled);
      if (next === "off" && enabled) await write("/api/evolution/settings", { enabled: false }, "演化已关闭");
      if (next !== "off" && !enabled) await write("/api/evolution/settings", { enabled: true }, "演化已开启");
      return;
    }
    const rollback = target.closest("[data-live-rollback]");
    if (rollback) {
      event.preventDefault();
      const version = rollback.dataset.liveRollback;
      openLiveModal("回滚到上一版？",
        "<p>回滚会重新下发该版本，并保留切换后新增的对话、更正与删除记录。</p>"
        + '<p class="meta">版本 · ' + esc(version) + "</p>", "确认回滚", async () => {
          await write("/api/persona/rollback", { version }, (result) => decisionToast(result, "已回滚"));
        });
      return;
    }
    const publish = target.closest("[data-live-publish]");
    if (publish) {
      event.preventDefault();
      const targetName = publish.dataset.livePublish;
      const persona = LIVE.snapshot.persona || {};
      const card = persona.card || {};
      await write("/api/persona/publish", { target: targetName, revision: card.revision || undefined },
        (result) => decisionToast(result, "已发布"));
      return;
    }
    const adopt = target.closest("[data-live-adopt]");
    if (adopt && SERVICE) { event.preventDefault(); toast("发起一次对话后会记录实际采用版本"); return; }
    if (adopt) {
      event.preventDefault();
      await write("/api/persona/adopt", {
        consumer: adopt.dataset.liveAdopt, version: adopt.dataset.version,
        receipt_id: rid("ui-adopt"),
      }, (result) => decisionToast(result, "使用方已采用"));
      return;
    }
    const freeze = target.closest("[data-live-freeze]");
    if (freeze) {
      event.preventDefault();
      await write("/api/persona/freeze", {
        section_id: freeze.dataset.liveFreeze,
        frozen: freeze.dataset.frozen !== "1",
      }, (result) => decisionToast(result, "冻结状态已更新"));
      return;
    }
    const editAll = target.closest("[data-live-edit-persona]");
    if (editAll) {
      event.preventDefault();
      openPersonaEditor();
      return;
    }
    const edit = target.closest("[data-live-edit]");
    if (edit) {
      event.preventDefault();
      const sectionId = edit.dataset.liveEdit;
      const persona = LIVE.snapshot.persona || {};
      const section = (persona.card.sections || []).find((row) => row.section_id === sectionId);
      if (!section) return;
      openLiveDrawer("编辑段落 · " + sectionLabel(sectionId), "基于第 " + (persona.card.revision || 0) + " 版",
        '<div class="detail-sec" style="margin-top:0"><h4>' + esc(sectionLabel(sectionId)) + "</h4>"
        + '<textarea data-live-edit-text="' + esc(sectionId) + '" rows="6" style="width:100%;padding:10px 12px;'
        + 'border:1px solid var(--line);background:rgba(255,253,248,.9);font:inherit;font-size:14.5px;line-height:1.7;'
        + 'color:var(--ink);resize:vertical">' + esc(section.text) + "</textarea></div>"
        + '<div class="detail-act"><button type="button" class="btn" data-live-editor-cancel>取消</button>'
        + '<button type="button" class="btn pri" data-live-editor-save>保存为新版本</button></div>');
      $("#drawer").dataset.baseRevision = String(persona.card.revision);
      return;
    }
    const save = target.closest("[data-live-editor-save]");
    if (save) {
      event.preventDefault();
      await savePersonaEditor();
      return;
    }
    const cancel = target.closest("[data-live-editor-cancel]");
    if (cancel) {
      event.preventDefault();
      const drawer = $("#drawer");
      if (drawer) drawer.classList.remove("on");
      const mask = $("#drawerMask");
      if (mask) mask.classList.remove("on");
      return;
    }
    const cleanKind = target.closest("[data-clean-kind]");
    if (cleanKind) {
      event.preventDefault();
      LIVE.cleanupKind = cleanKind.dataset.cleanKind || "all";
      LIVE.cleanupLimit = CLEAN_PAGE_SIZE;
      renderCleanup();
      return;
    }
    const cleanMore = target.closest("[data-clean-more]");
    if (cleanMore) {
      event.preventDefault();
      LIVE.cleanupLimit += CLEAN_PAGE_SIZE;
      renderCleanup();
      return;
    }
    const del = target.closest("[data-live-delete]");
    const retryCleanup = target.closest("[data-live-cleanup-retry]");
    if (retryCleanup) {
      event.preventDefault();
      await write("/v1/cleanup/retry", { deletion_id: retryCleanup.dataset.liveCleanupRetry }, "清理已重试");
      return;
    }
    const retractCorrection = target.closest("[data-live-correction-retract]");
    if (retractCorrection) {
      event.preventDefault();
      await write("/v1/cleanup/retract-correction", { correction_id: retractCorrection.dataset.liveCorrectionRetract }, "更正已撤回");
      return;
    }
    if (del) {
      event.preventDefault();
      const type = del.dataset.type;
      const id = del.dataset.liveDelete;
      openLiveModal("删除这条记忆？",
        "<p>删除会生成墓碑，并使派生的事实与反思失效。</p>"
        + '<p class="meta">' + esc(type) + " · " + esc(id) + "</p>", "删除", async () => {
          await write("/api/cleanup/delete", {
            type, id, reason: "webui delete", request_id: rid("ui-delete"),
          }, "已删除");
        });
      return;
    }
    const tombstones = target.closest("[data-live-tombstones]");
    if (tombstones) {
      event.preventDefault();
      const rows = (LIVE.snapshot.cleanup.tombstones || []);
      openLiveDrawer("墓碑", rows.length + " 条来源指纹",
        rows.length ? rows.map((row) => '<div class="rel-item"><span class="tag">' + esc(row.target_type)
          + "</span>" + esc(row.target_id) + "<b>" + esc(row.at ? relTime(row.at) : "—") + "</b></div>").join("")
          : '<div class="empty">还没有墓碑</div>');
      return;
    }
    const evolutionSwitch = target.closest("[data-live-evolution]");
    if (evolutionSwitch) {
      event.preventDefault();
      const next = evolutionSwitch.dataset.liveEvolution === "1";
      await write("/api/evolution/settings", { enabled: next }, "演化设置已更新");
      // Keep the switch and the mode chips consistent: turning evolution off
      // also parks the mode at "off", turning it on restores "propose".
      const mode = LIVE.snapshot && LIVE.snapshot.evolution ? LIVE.snapshot.evolution.mode : "off";
      if (!next && mode !== "off") await write("/api/evolution/mode", { mode: "off" }, "演化已关闭");
      if (next && mode === "off") await write("/api/evolution/mode", { mode: "propose" }, "演化已切到提议模式");
      return;
    }
    const exportSettings = target.closest("[data-live-export-settings]");
    if (target.closest("[data-live-maintain]")) {
      event.preventDefault();
      await write("/v1/maintenance", {}, "维护已完成");
      return;
    }
    if (exportSettings) {
      event.preventDefault();
      const payload = { engine: LIVE.engine, meta: LIVE.snapshot.meta, generated_at: new Date().toISOString() };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const anchor = document.createElement("a");
      anchor.href = URL.createObjectURL(blob);
      anchor.download = "rin-engine.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(anchor.href);
      toast("引擎信息已导出");
      return;
    }
    if (SERVICE && target.closest('a[href="/v1/export"]')) {
      event.preventDefault();
      const exported = await api("/v1/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = "rin-memory.json"; link.click(); URL.revokeObjectURL(url);
      return;
    }
    if (target.closest("[data-live-refresh]")) {
      event.preventDefault();
      await refresh();
      toast("已刷新");
      return;
    }
    if (target.closest("[data-live-modal-cancel]")) {
      event.preventDefault();
      closeLiveModal();
      return;
    }
    if (target.closest("[data-live-modal-confirm]")) {
      event.preventDefault();
      const action = LIVE.modalConfirm;
      closeLiveModal();
      if (action) await action();
      return;
    }
  });

  /* selection: run after the demo handlers, then override the detail */
  document.addEventListener("click", (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const eventRow = target.closest('[data-page="timeline"] .tl-row');
    if (eventRow && eventRow.dataset.liveEvent) {
      setTimeout(() => selectLiveEvent(eventRow.dataset.liveEvent), 0);
      return;
    }
    const reflRow = target.closest('.refl-row');
    if (reflRow && (reflRow.dataset.liveRefl || reflRow.dataset.liveGrowth)) {
      setTimeout(() => selectLiveReflection(reflRow.dataset.liveRefl || reflRow.dataset.liveGrowth), 0);
      return;
    }
    const propRow = target.closest('.prop-row');
    if (propRow && propRow.dataset.liveProposal) {
      setTimeout(() => selectLiveProposal(propRow.dataset.liveProposal), 0);
      return;
    }
    const verRow = target.closest('.ver-row');
    if (verRow && verRow.dataset.liveRevision) {
      setTimeout(() => showLiveRevision(verRow.dataset.liveRevision), 0);
    }
  });

  /* capture-phase overrides for demo-only controls */
  document.addEventListener("click", (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest("#dtDelete")) {
      event.preventDefault(); event.stopPropagation(); openDeleteEvent(); return;
    }
    if (target.closest("#dtCorrect")) {
      event.preventDefault(); event.stopPropagation(); openCorrectionModal(); return;
    }
    if (target.closest("[data-edit-persona]")) {
      event.preventDefault(); event.stopPropagation(); openPersonaEditor(); return;
    }
    if (target.closest("#showAll")) {
      event.preventDefault(); event.stopPropagation(); openPendingDrawer(); return;
    }
    if (target.closest("[data-drawer]")) {
      event.preventDefault(); event.stopPropagation(); openPublishDrawer();
    }
  }, true);

  bindChat();
  bindModelConfig();
  refresh();
  if (SERVICE) setInterval(() => { if (!LIVE.connected && !document.hidden) refresh(); }, 10000);
})();
