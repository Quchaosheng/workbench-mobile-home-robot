/* Assurance view. Read-only, and it says so when it cannot read.
 *
 * Three rules the rendering keeps, because a dashboard is where an honest register
 * most easily turns into a progress bar:
 *
 *  1. a gap renders as a gap, never as zero or as "coming soon";
 *  2. NOT_EXECUTED is a red state, not an empty cell - a case that never ran is not
 *     a case that passed;
 *  3. if the register cannot be read, the page says so and claims nothing. An empty
 *     table would read as "the runtime claims no invariants", which is false.
 */

const STATUS_CLASS = {
  ENFORCED: "is-enforced",
  NOT_IMPLEMENTED: "is-gap",
};

const COVERAGE_CLASS = {
  SIMULATED: "is-simulated",
  HARDWARE_TESTED: "is-hardware",
  NOT_EXECUTED: "is-not-executed",
};

const VERDICT_CLASS = {
  confirmed: "is-confirmed",
  insufficient_evidence: "is-insufficient",
  refuted: "is-refuted",
};

const VERDICT_LABELS = {
  confirmed: "confirmed",
  insufficient_evidence: "insufficient",
  refuted: "refuted",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function badge(text, className) {
  return el("span", `badge ${className || ""}`.trim(), text);
}

function showUnavailable(message) {
  const banner = document.getElementById("claim-scope");
  banner.textContent = "登记册不可读，本页不展示任何主张";
  banner.classList.add("scope-line-error");
  const shell = document.querySelector(".assurance-shell");
  const notice = el("div", "unavailable");
  notice.textContent = `无法读取登记册：${message}。空缺不代表没有主张，只代表读不到。`;
  shell.insertBefore(notice, shell.children[1]);
}

function renderGaps(gaps) {
  const list = document.getElementById("gap-list");
  list.replaceChildren();
  if (!gaps.length) {
    list.appendChild(el("li", "gap-none", "当前没有明示缺口。"));
    return;
  }
  for (const gap of gaps) {
    const item = el("li");
    const head = el("p", "gap-head");
    head.appendChild(el("span", "gap-id", gap.id));
    head.appendChild(el("span", "gap-label", gap.label || ""));
    item.appendChild(head);
    // The register's own sentence, kept verbatim. The Chinese label above says what
    // kind of thing is missing; this line is what the gate actually recorded.
    item.appendChild(el("p", "gap-detail", gap.detail || "未说明原因"));
    list.appendChild(item);
  }
}

function renderInvariants(invariants) {
  const body = document.getElementById("inv-body");
  body.replaceChildren();
  for (const entry of invariants) {
    const row = el("tr");
    const idCell = el("td", "cell-id", entry.id);
    idCell.dataset.label = "编号";
    row.appendChild(idCell);
    const statementCell = el("td", null, entry.statement);
    statementCell.dataset.label = "主张";
    row.appendChild(statementCell);

    const boundary = el("td");
    boundary.dataset.label = "运行时边界";
    boundary.appendChild(el("span", "cell-code", entry.runtime_boundary || "–"));
    row.appendChild(boundary);

    const status = el("td");
    status.dataset.label = "状态";
    status.appendChild(badge(entry.status_label, STATUS_CLASS[entry.status]));
    row.appendChild(status);

    const enforced = el("td");
    enforced.dataset.label = "执行点";
    if (entry.enforced_by) {
      enforced.appendChild(el("span", "cell-code cell-code-nowrap", entry.enforced_by));
    } else {
      enforced.appendChild(el("span", "cell-code cell-code-nowrap", "无（登记册未给出执行点）"));
    }
    row.appendChild(enforced);

    body.appendChild(row);
  }
}

function renderCases(cases) {
  const body = document.getElementById("case-body");
  body.replaceChildren();
  for (const item of cases) {
    const row = el("tr");
    const caseCell = el("td", "cell-id", item.case_id);
    caseCell.dataset.label = "用例";
    row.appendChild(caseCell);
    const faultCell = el("td", null, item.fault);
    faultCell.dataset.label = "故障";
    row.appendChild(faultCell);

    const state = item.expected_terminal_state || {};
    const stateCell = el("td", "cell-code", `${state.domain || "?"}:${state.state || "?"}`);
    stateCell.dataset.label = "期望终态";
    row.appendChild(stateCell);

    const verdict = el("td");
    verdict.dataset.label = "期望判定";
    verdict.appendChild(
      badge(VERDICT_LABELS[item.expected_verdict] || item.expected_verdict, VERDICT_CLASS[item.expected_verdict]),
    );
    row.appendChild(verdict);

    const coverage = el("td");
    coverage.dataset.label = "覆盖";
    coverage.appendChild(badge(item.coverage_label, COVERAGE_CLASS[item.coverage]));
    row.appendChild(coverage);

    body.appendChild(row);
  }
}

function renderCounts(counts, register) {
  document.getElementById("inv-enforced").textContent = counts.enforced;
  document.getElementById("inv-total").textContent = counts.invariants;
  document.getElementById("case-executed").textContent = counts.executed;
  document.getElementById("case-total").textContent = counts.cases;
  document.getElementById("gap-count").textContent = counts.not_implemented + counts.not_executed;
  document.getElementById("claim-scope").textContent = register.claim_scope;
  document.getElementById("inv-foot").textContent =
    counts.not_implemented > 0 ? `已强制执行 · ${counts.not_implemented} 条未实现` : "已强制执行";
  document.getElementById("case-foot").textContent =
    counts.not_executed > 0 ? `已执行 · ${counts.not_executed} 个未执行` : "已执行";
  document.getElementById("source-note").textContent =
    `数据来源：${register.invariants_version} / ${register.matrix_version}，` +
    `登记状态 ${register.register_status || "unknown"}。本页为只读投影，不重新判定任何主张。`;
}

async function load() {
  let payload;
  try {
    const response = await fetch("/api/assurance", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body && body.message) detail = body.message;
      } catch (error) {
        /* the status line is enough */
      }
      throw new Error(detail);
    }
    payload = await response.json();
  } catch (error) {
    showUnavailable(error.message || String(error));
    return;
  }
  renderCounts(payload.counts, payload);
  renderGaps(payload.gaps || []);
  renderInvariants(payload.invariants || []);
  renderCases(payload.cases || []);
}

document.addEventListener("DOMContentLoaded", load);
