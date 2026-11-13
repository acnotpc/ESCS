const course = "13168722425";
const views = ["Matched", "Missing Email", "Duplicate or Combined", "Attendance Exception", "Evidence Incomplete", "Ready to Add", "Needs Review"];
const starter = [{
  name: "Romisha Dhaka", email: "", source: "Trainer portal · reported by Andrew",
  evidence: "Andrew reported two learner entries and later confirmed they had been combined.",
  discrepancy: "The retained email, per-day attendance and Weebly group membership still need source verification.",
  action: "Verify the surviving portal profile and Jotform email. Compare each course day’s events and check the matching address in Weebly.",
  attendance: "9 and 10 November · arrival, lunch and end-of-day records unverified.",
  weebly: "Unverified",
  views: ["Duplicate or Combined", "Evidence Incomplete", "Needs Review"],
  links: []
}];
const $ = id => document.getElementById(id);
let learners = starter;
let active = "All learners";
function element(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}
function render() {
  $("total").textContent = learners.length;
  $("views").replaceChildren(...["All learners", ...views].map(view => {
    const count = view === "All learners" ? learners.length : learners.filter(row => row.views.includes(view)).length;
    const button = element("button", view + " (" + count + ")");
    button.setAttribute("aria-pressed", String(active === view));
    button.onclick = () => {
      active = view;
      render();
      Array.from($("views").children).find(node => node.getAttribute("aria-pressed") === "true").focus();
    };
    return button;
  }));
  const query = $("query").value.trim().toLowerCase();
  const visible = learners.filter(row => (active === "All learners" || row.views.includes(active)) &&
    [row.name, row.email, row.source, row.discrepancy, ...row.views].some(value => value.toLowerCase().includes(query)));
  $("results").textContent = visible.length ? visible.length + " of " + learners.length + " learner rows shown" : "No recorded findings in this view. Unverified entries may still exist.";
  $("rows").replaceChildren(...visible.map(row => {
    const tr = document.createElement("tr");
    const details = document.createElement("details");
    details.append(element("summary", row.name), element("small", row.email || "Email unverified"));
    const list = document.createElement("dl");
    const fields = {Course: course, Evidence: row.evidence, Attendance: row.attendance, Discrepancy: row.discrepancy, "Next action": row.action};
    for (const [label, value] of Object.entries(fields)) list.append(element("dt", label), element("dd", value || "Unverified"));
    details.append(list);
    for (const link of row.links) {
      const anchor = element("a", link.label);
      anchor.href = link.url;
      details.append(anchor, document.createElement("br"));
    }
    const learner = tr.insertCell();
    learner.append(details);
    tr.insertCell().textContent = row.source;
    const findings = tr.insertCell();
    for (const view of row.views) {
      const badge = element("span", view);
      badge.className = "badge";
      findings.append(badge);
    }
    tr.insertCell().textContent = row.weebly;
    return tr;
  }));
}
$("example").textContent = JSON.stringify({course, learners: starter}, null, 2);
$("query").addEventListener("input", render);
$("reset").onclick = () => {
  learners = starter;
  active = "All learners";
  $("query").value = "";
  $("file").value = "";
  $("snapshot").textContent = "Starter snapshot · 9 Nov 2026";
  $("importStatus").textContent = "Starter snapshot restored.";
  render();
};
$("file").onchange = async () => {
  const file = $("file").files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const fields = ["name", "email", "source", "evidence", "discrepancy", "action", "attendance", "weebly"];
    const valid = row => row && fields.every(key => typeof row[key] === "string") && row.name.trim() &&
      Array.isArray(row.views) && row.views.every(view => views.includes(view)) &&
      Array.isArray(row.links) && row.links.every(link => link && typeof link.label === "string" &&
        typeof link.url === "string" && new URL(link.url).protocol === "https:");
    if (data.course !== course || !Array.isArray(data.learners) || !data.learners.every(valid)) throw new Error("Invalid snapshot");
    learners = data.learners;
    active = "All learners";
    $("query").value = "";
    $("snapshot").textContent = "Session snapshot · supplied by reviewer";
    $("importStatus").textContent = "Snapshot loaded for course " + course + ".";
    render();
  } catch {
    $("importStatus").textContent = "Could not open the snapshot. Check the course reference, fields, view names and HTTPS links against the example.";
  }
};
render();
