(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败" || state === "裁撤中"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${esc(state)}</span>`;
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。数字与后续对账同源。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${p.archive_uri || "—"}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。执行结果写回分区存储态与执行流水。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="7" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        const original = btn.textContent;
        try {
          btn.disabled = true;
          btn.textContent = "落笔中";
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        } catch (err) {
          // 409：状态不可执行，或另一名值班正持锁写；提示后保留页面不跳转。
          window.alert(err.message);
          btn.disabled = false;
          btn.textContent = original;
          return;
        }
        await renderJudgments();
      };
    });
  }

  function kpiCard(label, value, tone, hint) {
    return `
      <div class="kpi ${tone || ""}">
        <div class="kpi-label">${esc(label)}</div>
        <div class="kpi-value">${esc(value)}</div>
        ${hint ? `<div class="kpi-hint">${esc(hint)}</div>` : ""}
      </div>`;
  }

  function mismatchBar(snap) {
    const n = snap.counts.mismatch;
    if (n === 0) {
      return `<div class="statusbar ok" role="status">
        <span class="status-dot" aria-hidden="true"></span>
        <span>账实相符：已完成裁决与实物分区存储态全部对齐，错位 0 条。</span>
      </div>`;
    }
    const items = snap.mismatches
      .map(
        (m) => `<li>裁决 #${m.judgment_id}（${esc(m.decided_action)}）→ 分区 #${m.partition_id}
          ${esc(m.domain_code)} / ${esc(m.biz_date)} 实际为「${esc(m.storage_state)}」</li>`
      )
      .join("");
    return `<div class="statusbar bad" role="alert">
      <span class="status-dot" aria-hidden="true"></span>
      <div>
        <div>发现 ${n} 条错位：裁决已完成但实物分区存储态不符，需复核后重新裁撤。</div>
        <ul class="mismatch-list">${items}</ul>
      </div>
    </div>`;
  }

  async function renderReconcile() {
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
        <span id="rec-msg" class="rec-msg" role="status"></span>
      </div>
      <div id="rec-body"><div class="panel empty">正在冻结对账快照…</div></div>`;

    async function loadSnapshot() {
      // 读页瞬间冻结：卡片、执行列表、分区详情全部来自同一个快照接口，
      // 前端不再为三块各发一次请求，杜绝三个瞬时读点拧巴。
      const data = await api("/api/reconcile/snapshot");
      const snap = data.snapshot;
      const c = snap.counts;
      const latest = snap.latest_report;
      const body = document.getElementById("rec-body");
      body.innerHTML = `
        <section class="panel">
          <h2>对账摘要
            <span class="snapshot-at">快照冻结于 ${esc(snap.snapshot_at)}（${esc(snap.count_basis)}）</span>
          </h2>
          <div class="kpi-row">
            ${kpiCard("在线分区", c.online, "warn")}
            ${kpiCard("已归档", c.archived, "ok")}
            ${kpiCard("已销毁", c.destroyed, "ok")}
            ${kpiCard("裁撤中", c.reconciling, "bad")}
            ${kpiCard("分区合计", c.partition_total, "", `四态同源计数${snap.count_aligned ? "，与分区行数对齐" : "，与分区行数不符"}`)}
            ${kpiCard("待执行裁决", c.pending_judgment, "warn")}
            ${kpiCard("错位", c.mismatch, c.mismatch ? "bad" : "ok")}
          </div>
          ${mismatchBar(snap)}
          <p class="sub">
            计数口径钉死：卡片数字、最新报告与下方分区行数同出一个读事务；
            ${latest
              ? `最新报告 #${latest.id}（基准日 ${esc(latest.as_of_date)}）：${esc(latest.summary)}`
              : "尚未生成对账报告。"}
          </p>
        </section>

        <section class="panel">
          <h2>执行列表 <span class="snapshot-at">同快照 · ${esc(snap.execute_runs.length)} 条流水</span></h2>
          <table>
            <thead><tr><th>流水</th><th>裁决</th><th>动作</th><th>结果态</th><th>归档地址</th><th>结果</th><th>说明</th></tr></thead>
            <tbody>
              ${
                snap.execute_runs.length
                  ? snap.execute_runs
                      .map(
                        (r) => `<tr>
                  <td>${r.id}</td>
                  <td>#${r.judgment_id}</td>
                  <td>${esc(r.action)}</td>
                  <td>${tag(r.result_state)}</td>
                  <td class="mono">${esc(r.archive_uri || "—")}</td>
                  <td>${r.ok === 1 ? `<span class="tag ok">成功</span>` : `<span class="tag bad">失败</span>`}</td>
                  <td>${esc(r.detail)}</td>
                </tr>`
                      )
                      .join("")
                  : `<tr><td colspan="7" class="empty">尚无执行流水</td></tr>`
              }
            </tbody>
          </table>
        </section>

        <section class="panel">
          <h2>分区详情 <span class="snapshot-at">同快照 · ${esc(snap.partitions.length)} 个分区</span></h2>
          <table>
            <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th></tr></thead>
            <tbody>
              ${snap.partitions
                .map(
                  (p) => `<tr>
                <td class="mono">${esc(p.domain_code)}</td>
                <td class="mono">${esc(p.biz_date)}</td>
                <td>${p.row_count}</td>
                <td>${tag(p.storage_state)}</td>
                <td class="mono">${esc(p.archive_uri || "—")}</td>
              </tr>`
                )
                .join("")}
            </tbody>
          </table>
        </section>

        <section class="panel">
          <h2>历史对账报告</h2>
          <table>
            <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>裁撤中</th><th>合计</th><th>待裁决</th><th>错位</th><th>快照时刻</th><th>摘要</th></tr></thead>
            <tbody id="report-rows">
              <tr><td colspan="11" class="empty">加载中…</td></tr>
            </tbody>
          </table>
        </section>`;

      const reports = await api("/api/reconcile-reports");
      const rows = document.getElementById("report-rows");
      rows.innerHTML = reports.items.length
        ? reports.items
            .map(
              (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.reconciling_count}</td>
              <td>${r.partition_total}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td class="mono">${esc(r.snapshot_at || r.created_at)}</td>
              <td>${esc(r.summary)}</td>
            </tr>`
            )
            .join("")
        : `<tr><td colspan="11" class="empty">尚无对账报告</td></tr>`;
    }

    await loadSnapshot();

    document.getElementById("btn-rec").onclick = async () => {
      const msg = document.getElementById("rec-msg");
      const as_of_date = document.getElementById("as-of-r").value.trim();
      msg.className = "rec-msg";
      msg.textContent = "落笔中（已加写锁）…";
      try {
        const r = await api("/api/reconcile-reports", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        msg.className = "rec-msg ok";
        msg.textContent = `报告 #${r.report.id} 已落库，快照已刷新`;
      } catch (err) {
        // 409：另一人持锁或混数被拦，不刷新页面，保留当前冻结快照。
        msg.className = "rec-msg bad";
        msg.textContent = err.message;
        return;
      }
      await loadSnapshot();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
