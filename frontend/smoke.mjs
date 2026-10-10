/**
 * Renders the built bundle in jsdom and visits every view.
 *
 * `tsc` and `vite build` both pass on a payload whose shape has drifted: the
 * JSON is not validated at runtime, so a missing key is `undefined` at the point
 * of use and React unmounts the whole tree — a blank page with a clean build.
 * This catches that, which is the only failure mode the other two cannot see.
 */
import fs from "node:fs"
import { JSDOM } from "jsdom"

const bundle = fs.readdirSync("dist/assets").find((f) => f.endsWith(".js"))
const cssBundle = fs.readdirSync("dist/assets").find((f) => f.endsWith(".css"))
// SMOKE_DATA checks an export before it is published.
const DATA = process.env.SMOKE_DATA || "public/data"
// The whole fleet as the loader assembles it, for the checks below that compare
// against the payload directly.
const index = JSON.parse(fs.readFileSync(`${DATA}/index.json`, "utf8"))
// A file the index lists but that cannot be read is left out, as the loader
// does; the Overview must then name it (checked below).
const broken = []
const readCluster = (c) => {
  try { return JSON.parse(fs.readFileSync(`${DATA}/${c.file}`, "utf8")).cluster }
  catch { broken.push(c.env); return null }
}
const fleet = JSON.stringify({
  schema: Math.min(...index.periods.flatMap((p) => p.clusters.map((c) => c.schema ?? 0))),
  generatedAt: index.generatedAt, discount: index.discount, uncollected: index.uncollected,
  periods: index.periods.map((p) => ({
    id: p.id, label: p.label, end: p.end, collectedAt: p.collectedAt,
    clusters: p.clusters.map(readCluster).filter(Boolean),
  })).filter((p) => p.clusters.length),
})
const dom = new JSDOM(fs.readFileSync("dist/index.html", "utf8"), {
  runScripts: "outside-only",
  pretendToBeVisual: true,
  url: "http://localhost/",
})
const { window } = dom
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
// Answer per path. Returning the fleet payload for every request hides exactly
// the kind of shape mismatch this test exists to catch.
window.fetch = async (url) => {
  const path = String(url)
  if (path.startsWith("data/")) {
    const file = `${DATA}/${decodeURIComponent(path.slice("data/".length))}`
    if (fs.existsSync(file)) {
      let body = fs.readFileSync(file, "utf8")
      if (process.env.SMOKE_STALE_TRENDS && path.includes("/trends/")) {
        // Pretend every trend predates the current collection method.
        const doc = JSON.parse(body)
        delete doc.schema
        body = JSON.stringify(doc)
      }
      return { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) }
    }
  }
  // A static host serving a single-page app answers an unknown path with
  // index.html and a 200, not a 404. Reproduce that, because a mock that 404s
  // lets the app assume a failure mode it will never actually see.
  const fallback = fs.readFileSync("dist/index.html", "utf8")
  return { ok: true, status: 200, text: async () => fallback, json: async () => JSON.parse(fallback) }
}
window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
window.cancelAnimationFrame = (id) => clearTimeout(id)

const errors = []
window.addEventListener("error", (e) => errors.push(e.error?.stack || e.message))
window.console.error = (...a) => errors.push(a.map(String).join(" "))

window.eval(fs.readFileSync(`dist/assets/${bundle}`, "utf8"))
const wait = (ms) => new Promise((r) => setTimeout(r, ms))
await wait(600)

const root = window.document.getElementById("root")
const fail = (msg) => {
  console.error(`FAIL  ${msg}`)
  process.exitCode = 1
}

if (!root?.children.length) fail("nothing rendered")
if (broken.length) {
  const text = root?.textContent || ""
  if (!/could not be loaded/.test(text) || !broken.every((e) => text.includes(e)))
    fail(`unloadable cluster(s) ${broken.join(", ")} not named on the Overview`)
  else console.log(`ok    layout     unloadable ${broken.join(", ")} named; the rest loaded`)
}

for (const label of ["Overview", "Clusters", "Recommendations", "Workloads", "Compare"]) {
  const button = [...window.document.querySelectorAll("button")].find(
    (b) => b.textContent?.trim().startsWith(label),
  )
  if (!button) {
    fail(`no nav button for ${label}`)
    continue
  }
  button.click()
  await wait(250)
  const text = (root.textContent || "").trim()
  if (text.length < 200) fail(`${label} rendered ${text.length} chars`)
  else if (label === "Clusters" && !text.includes("Cluster Autoscaler"))
    fail("Clusters view missing the autoscaler panel")
  else if (label === "Clusters" && !/blocked by pods CA cannot evict/.test(text))
    fail("autoscaler table does not say which nodes a veto class holds")
  else if (label === "Workloads" && !/no limit|limit \d/.test(text))
    fail("Workloads does not say whether a container has a CPU limit")
  else if (label === "Workloads" && !/avg \d+ · peak \d+/.test(text))
    fail("Workloads replica column does not distinguish running from the 30-day peak")
  else console.log(`ok    ${label.padEnd(10)} ${text.length} chars`)
}

// The cloud filter: every cluster carries its cloud's mark, and choosing a
// cloud leaves only that cloud's clusters in the picker.
{
  const nav = (label) => [...window.document.querySelectorAll("button")]
    .find((b) => b.textContent?.trim().startsWith(label))
  nav("Clusters")?.click()
  await wait(250)
  // Marks beside cluster names: every button's but the filter's own, which
  // shows the chosen cloud's mark too.
  const marks = () => [...root.querySelectorAll("button:not(#cloud-select) [role=img]")].map((m) => m.getAttribute("aria-label"))
  const clouds = new Set(JSON.parse(fleet).periods.flatMap((p) => p.clusters.map((c) => c.cloud || "aws")))
  // The filter is a Radix Select: opened and chosen from the keyboard, as a
  // screen-reader user would. jsdom lacks the few DOM calls it makes.
  window.Element.prototype.scrollIntoView ??= () => {}
  window.Element.prototype.hasPointerCapture ??= () => false
  window.Element.prototype.releasePointerCapture ??= () => {}
  window.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }
  const key = (el, k) => el.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true }))
  const trigger = () => window.document.getElementById("cloud-select")
  const choose = async (startsWith) => {
    key(trigger(), "Enter")
    await wait(150)
    const opts = [...window.document.querySelectorAll("[role=option]")]
    // An option's text begins with its mark's accessible name ("Amazon Web
    // Services"), so the label is looked for anywhere in it.
    const opt = opts.find((o) => (o.textContent || "").includes(startsWith))
    if (!opt) return { opts }
    key(opt, "Enter")
    await wait(200)
    return { opts, chosen: opt }
  }
  if (!marks().length) fail("no cloud mark beside the cluster names")
  else if (clouds.size > 1 && !trigger()) fail(`${clouds.size} clouds but no cloud filter`)
  else if (trigger()) {
    const all = marks().length
    const narrowed = []
    for (const cloud of [...clouds].sort()) {
      const label = cloud.toUpperCase()
      const { opts, chosen } = await choose(`${label} (`)
      const optionMarks = opts.filter((o) => o.querySelector("[role=img]")).length
      if (!chosen) { fail(`no ${label} option in the cloud filter`); continue }
      if (optionMarks !== clouds.size) fail(`cloud filter options carry ${optionMarks} marks, not ${clouds.size}`)
      if (!trigger().querySelector("[role=img]")) fail(`the closed filter does not show ${label}'s mark`)
      const seen = new Set(marks())
      if (seen.size !== 1) fail(`cloud filter ${label} still shows ${[...seen].join(", ")}`)
      else narrowed.push(`${cloud} ${marks().length}/${all}`)
    }
    await choose("All clouds")
    if (marks().length !== all) fail("back to all clouds does not restore every cluster")
    else console.log(`ok    clouds     marked, in the filter too; it narrows to ${narrowed.join(", ")}`)
  } else console.log("ok    clouds     one cloud: marked, no filter needed")
}

// The scenario dialog is the view's whole purpose, so exercise it rather than
// trusting that a row renders.
const scenariosBtn = [...window.document.querySelectorAll("button")].find((b) =>
  b.textContent?.trim().startsWith("Recommendations"),
)
scenariosBtn?.click()
await wait(250)
// Scenario labels are generated from the change they represent, so match the
// first non-baseline row rather than a fixed label.
// Recommendations lists options per node group; a row opens that option's
// detail. One dialog, not a dialog inside a dialog.
const rows = [...window.document.querySelectorAll('tr[role="button"]')]
if (!rows.length) fail("no clickable option row")
else {
  // Only options that change requests list the containers behind them, so find
  // one that does rather than assuming the first row is that kind.
  let opened = false
  for (const row of rows.slice(0, 12)) {
    row.click()
    await wait(300)
    const d = window.document.querySelector('[role="dialog"]')
    if (d?.querySelector('button[aria-label^="Usage trend"]')) { opened = true; break }
    window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    )
    await wait(120)
  }
  if (!opened) {
    rows[0].click()
    await wait(350)
  }
  const dialogs = [...window.document.querySelectorAll('[role="dialog"]')]
  if (dialogs.length !== 1) fail(`expected exactly one dialog, saw ${dialogs.length}`)
  const text = dialogs[0]?.textContent ?? ""
  if (!/How the pods (land|sit today|would land)/.test(text))
    fail("option dialog missing the packing section")

  else console.log(`ok    option     ${text.trim().length} chars`)
  {
    const sel = dialogs[0]?.querySelector('[aria-label="Your selection"]')
    if (opened && !sel) fail("an option with request changes shows no selection figures")
    else if (sel?.closest(".overflow-y-auto")) fail("the selection figures are in the scrolling body, not the header")
    else if (sel && !/\d+ nodes/.test(sel.textContent || "")) fail("the header selection has no node count")
    else if (sel) console.log(`ok    selection  in the header: ${sel.textContent?.replace(/\s+/g, " ").trim().slice(0, 60)}`)
  }

  // A request row opens to its utilisation projection, and trying a value there
  // must move the tiles -- a slider that changes nothing is the failure.
  {
    const dlg = window.document.querySelector('[role="dialog"]')
    const openBtn = dlg?.querySelector('button[aria-controls^="reqproj-"]')
    if (!openBtn && opened) fail("request rows cannot be expanded to a projection")
    else if (openBtn) {
      openBtn.click()
      await wait(400)
      const body = () => window.document.querySelector('[role="dialog"]')?.textContent || ""
      const slider = window.document.querySelector('[role="dialog"] input[type="range"][id^="req-"]')
      if (/No projection:/.test(body()) && !slider)
        console.log("ok    request    projection (no trend file for this container)")
      else if (!slider || !/Try (CPU|Memory) request/.test(body()))
        fail("request projection did not render its slider")
      else {
        const before = body()
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set
        setter.call(slider, slider.max)
        slider.dispatchEvent(new window.Event("input", { bubbles: true }))
        await wait(250)
        if (body() === before) fail("moving the request slider changed nothing")
        else {
          const panel = window.document.querySelector('[id^="reqproj-"]')
          const tabs = [...(panel?.querySelectorAll('[role="tab"]') ?? [])].map((t) => t.textContent?.trim())
          const charts = panel?.querySelectorAll('svg[role="img"]').length ?? 0
          if (tabs.includes("All") || !tabs.every((t) => t === "CPU" || t === "Memory"))
            fail(`request projection tabs are ${JSON.stringify(tabs)}`)
          else if (charts !== 1) fail(`request projection shows ${charts} charts at once; one per tab`)
          else console.log(`ok    request    projection responds to the slider, one chart, tabs ${tabs.join("/")}`)
        }
        const back = [...window.document.querySelectorAll('[role="dialog"] button')]
          .find((b) => b.textContent?.trim() === "back to recommended")
        back?.click()
        await wait(150)
      }
      openBtn.click()
      await wait(100)
    }
  }

  // The chart button on a driver row must reach the trend without the reader
  // going to Workloads and searching, and must hand the option list back when
  // the trend closes -- not stack one dialog on another.
  const trendBtn = window.document.querySelector('[role="dialog"] button[aria-label^="Usage trend"]')
  if (!trendBtn && !opened)
    fail("no option in this dataset lists the containers behind it")
  else if (!trendBtn) fail("no per-container trend button in the option dialog")
  else {
    trendBtn.click()
    await wait(350)
    const open = [...window.document.querySelectorAll('[role="dialog"]')]
    if (open.length !== 1) fail(`trend opened ${open.length} dialogs; it must replace, not stack`)
    else if (!/Spread across replicas/.test(open[0].textContent || ""))
      fail("trend button did not open the usage trend")
    else {
      window.document.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      )
      await wait(300)
      const back = window.document.querySelector('[role="dialog"]')?.textContent || ""
      if (!/How the pods (land|sit today|would land)/.test(back))
        fail("closing the trend did not return to the option dialog")
      else console.log("ok    trend link  option -> trend -> option")
      window.document.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      )
      await wait(200)
    }

  // An option that says "lower minReplicas" must name the HPAs. The sections
  // used to be gated on the scenario key, so a combined right-size + replica
  // option showed the container table and hid the HPA one.
  {
    const rows = [...window.document.querySelectorAll('tr[role="button"]')]
    for (const row of rows.slice(0, 14)) {
      if (!/minreplicas|hpa/i.test(row.textContent || "")) continue
      row.click()
      await wait(300)
      const d = window.document.querySelector('[role="dialog"]')?.textContent || ""
      if (/lower minReplicas/.test(d) && !/minReplicas|HPA/.test(d.replace(/lower minReplicas/g, "")))
        fail("a replica option does not name the HPAs it changes")
      else if (/lower minReplicas/.test(d) && !/-hpa|hpa\b/i.test(d))
        fail("a replica option lists no HPA names")
      else console.log("ok    hpa detail replica option names its HPAs")
      window.document.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      )
      await wait(150)
      break
    }
  }

  // An instance-type change moves the disk with it: the option must say what
  // local storage each side has and how full the nodes get -- and chart the
  // utilisation as one chart with a tab per resource, not a stack of panels.
  {
    const rows = [...window.document.querySelectorAll('tr[role="button"]')]
    const esc = () => window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
    let seen = false
    for (const row of rows.slice(0, 20)) {
      row.click()
      await wait(250)
      const dlg = window.document.querySelector('[role="dialog"]')
      const d = dlg?.textContent || ""
      if (!/Node configuration/.test(d)) { esc(); await wait(120); continue }
      seen = true
      const tabs = [...dlg.querySelectorAll('[role="tablist"] [role="tab"]')].map((t) => t.textContent?.trim())
      const charts = () => dlg.querySelectorAll('svg[role="img"][aria-label*="node group"], svg[role="img"][aria-label*="Node count"]')
      if (!/Instance storage/.test(d)) fail("an instance-type option does not show instance storage")
      else if (!/Network bandwidth/.test(d) || !/Busiest node, busiest hour/.test(d))
        fail("an instance-type option does not show network bandwidth and the traffic it must carry")
      else if (/Local storage/.test(d))
        fail("the separate Local storage section is back; disk use belongs in the utilisation chart")
      else if (!tabs.includes("Disk (kubelet)") && !/disk not measured/.test(d))
        fail("an instance-type option does not chart disk use before and after")
      else if (dlg.querySelector('[aria-label="Your selection"]')?.closest(".overflow-y-auto"))
        fail("the selection's node count and saving scroll away with the body; they belong in the header")
      else if (!/Utilisation over the window/.test(d)) fail("a node-group option does not chart utilisation")
      else if (!["All", "CPU", "Memory", "Nodes"].every((t) => tabs.includes(t)))
        fail(`utilisation chart tabs are ${JSON.stringify(tabs)}`)
      else if (charts().length !== 1) fail(`utilisation shows ${charts().length} charts at once; one per tab`)
      else {
        const before = charts()[0].getAttribute("aria-label")
        // Radix activates a tab on mousedown, not click.
        const mem = [...dlg.querySelectorAll('[role="tab"]')].find((t) => t.textContent?.trim() === "Memory")
        mem.dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true, button: 0 }))
        await wait(150)
        const after = charts()[0]?.getAttribute("aria-label")
        if (!after || after === before || !/^Memory/.test(after)) fail("the Memory tab did not switch the chart")
        else console.log("ok    storage    instance-type option: disk and network before/after, one utilisation chart with tabs")
      }
      esc()
      await wait(150)
      break
    }
    if (!seen) console.log("ok    storage    (no instance-type option in the first rows)")
  }

  // The instance-type table is there when asked for, not in the way: closed
  // on open, revealed from the header, and it simulates a type the export
  // does not carry from specs the reader confirms.
  {
    const esc = () => window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
    const setVal = (el, v) => {
      const proto = Object.getPrototypeOf(el)
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v)
      el.dispatchEvent(new window.Event("input", { bubbles: true }))
    }
    let done = false
    for (const row of [...window.document.querySelectorAll('tr[role="button"]')].slice(0, 20)) {
      row.click()
      await wait(250)
      const dlg = window.document.querySelector('[role="dialog"]')
      const link = [...(dlg?.querySelectorAll("button") ?? [])].find((b) => b.textContent === "compare instance types")
      if (!link) { esc(); await wait(120); continue }
      done = true
      if (/Other instance types/.test(dlg.textContent)) { fail("the instance-type table is open before it is asked for"); break }
      link.click(); await wait(250)
      const table = dlg.querySelector("#instance-types")
      const typeRows = [...(table?.querySelectorAll('tr[role="button"]') ?? [])]
      if (!typeRows.length) { fail("compare instance types opened no table"); break }
      const other = typeRows.find((r) => r.getAttribute("aria-pressed") === "false")
      other.click(); await wait(300)
      if (!/Your pick: /.test(dlg.textContent)) { fail("picking another type does not re-price the option"); break }
      const input = dlg.querySelector("#custom-type")
      setVal(input, "m7i.2xlarge"); await wait(100)
      ;[...dlg.querySelectorAll("button")].find((b) => b.textContent === "Simulate").click(); await wait(200)
      const vcpu = dlg.querySelector('input[aria-label="vCPU"]')
      const mem = dlg.querySelector('input[aria-label="memory GiB"]')
      if (!vcpu || vcpu.value !== "8" || mem.value !== "32") {
        fail(`m7i.2xlarge sketched as ${vcpu?.value} vCPU / ${mem?.value} GiB, not 8 / 32`); break
      }
      setVal(dlg.querySelector('input[aria-label="price per hour"]'), "0.4032"); await wait(100)
      ;[...dlg.querySelectorAll("button")].find((b) => b.textContent === "Simulate m7i.2xlarge").click()
      await wait(300)
      if (!/Your pick: m7i\.2xlarge/.test(dlg.textContent)) fail("a typed-in instance type was not simulated")
      else if (!/your specs/.test(dlg.textContent)) fail("a typed-in type is not marked as the reader's specs")
      else console.log("ok    types      hidden until asked; pick re-prices; a typed-in type simulates")
      esc(); await wait(150)
      break
    }
    if (!done) console.log("ok    types      (no option with a type table in the first rows)")
  }

  // An option CA cannot deliver must still be listed, badged, and carry the
  // specific fix -- it competes with replacing the node group, and that choice
  // cannot be made if the row is filtered out.
  {
    window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    )
    await wait(200)
    // Not every cluster has one: prod-us2's went once volume zones were
    // enforced. Try each cluster's tab before calling it missing.
    let listed = window.document.body.textContent || ""
    for (const b of [...window.document.querySelectorAll("button.font-mono")]) {
      if (/blocked on \d+ node/.test(listed)) break
      b.click()
      await wait(250)
      listed = window.document.body.textContent || ""
    }
    if (!/blocked on \d+ node/.test(listed))
      fail("no drain-blocked option is listed in Recommendations")
    else {
      const row = [...window.document.querySelectorAll('tr[role="button"]')].find((r) =>
        /blocked on \d+ node/.test(r.textContent || ""),
      )
      row?.click()
      await wait(300)
      const d = window.document.querySelector('[role="dialog"]')?.textContent || ""
      if (!/Unblock \d+ node/.test(d) || !/will not empty a node while any of these pods/.test(d))
        fail("blocked option's dialog does not explain what is blocking it")
      else if (!/Or drain by hand/.test(d) || !/kubectl drain \S+ --ignore-daemonsets/.test(d))
        fail("blocked option's dialog does not offer the one-off manual drain")
      else if (!/safe-to-evict=true|PodDisruptionBudget/.test(d))
        fail("blocked option's dialog does not name the fix")
      else if (/add a PodDisruptionBudget/.test(d) && !/replica/.test(d))
        fail("a PDB is suggested without showing the replica count that decides it")
      else console.log("ok    blocked    option listed with its blockers and fixes")
      window.document.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      )
      await wait(150)
    }
  }


  }
}

// The simulator has to actually simulate: tick a container, and the node count
// or the cost must move. A view that renders but computes nothing is the thing
// this check exists to catch.
{
  const simBtn = [...window.document.querySelectorAll("button")].find((b) =>
    b.textContent?.trim().startsWith("Simulator"),
  )
  if (!simBtn) fail("no Simulator view")
  else {
    simBtn.click()
    await wait(350)
    const read = () => (window.document.body.textContent || "")
    const before = read()
    if (/disagrees with the analysis engine/.test(before))
      fail("simulator reports its packer has diverged from the engine")
    // The first group is whichever comes first, which may have four pods. Pick
    // the busiest one, so "the selection changes the packing" is actually
    // exercised rather than trivially true.
    const groupBtns = [...window.document.querySelectorAll("button")].filter((b) =>
      /^[a-z][a-z-]*-\d+$/.test(b.textContent?.trim() || ""),
    )
    // The group with the most nodes today, then the most rows: it has room to
    // shrink. The most rows alone found resources-2, which three volume zones
    // hold at three nodes whatever is ticked.
    const nodesOf = Object.fromEntries(
      JSON.parse(fleet).periods[0].clusters[0].nodegroups.map((g) => [g.name, g.sim?.nodesNow ?? 0]))
    let best = null
    for (const b of groupBtns) {
      b.click()
      await wait(250)
      const n = window.document.querySelectorAll('input[type="checkbox"]').length
      const size = nodesOf[b.textContent?.trim() || ""] ?? 0
      if (!best || size > best.size || (size === best.size && n > best.n)) best = { b, n, size }
    }
    best?.b.click()
    await wait(300)
    const baseline = read()
    const boxes = [...window.document.querySelectorAll('input[type="checkbox"]')]
    if (boxes.length < 10) fail(`busiest group shows only ${boxes.length} selectable rows`)
    else {
      // Every row, not the first twelve: the twelve that happen to sort first
      // are small workloads whose changes genuinely do not move a 25-node
      // group, so asserting on them tested the assertion rather than the code.
      //
      // click(), not `.checked = true`: React 18 keeps its own value tracker
      // and suppresses the synthetic onChange when the property is set direct.
      for (const b of boxes) b.click()
      await wait(350)
      const after = read()
      if (after === baseline) fail("ticking containers changed nothing in the simulator")
      else if (!/Selected/.test(after)) fail("simulator lost its summary")
      else {
        // A selection that changes nothing but the counter is not a simulation.
        // Read the attribute, not the body text: the tile's value and its
        // subtitle concatenate, and "25 → 18" followed by "6.0 cores" scrapes
        // as "25 → 186".
        const tile = window.document.querySelector("[data-nodes]")
        const [from, to] = (tile?.getAttribute("data-nodes") || "0-0").split("-").map(Number)
        if (!(to < from)) fail(`selection repacked ${from} nodes to ${to}; expected fewer`)
        else console.log(`ok    simulator ${boxes.length} rows; selection repacked ${from} → ${to}`)
      }
    }
  }
}

// The console packs pods itself for the selection simulator, which is the one
// place the money math exists twice. Every group ships cases packed by the
// Python engine; the port must reproduce all of them.
{
  const { checkFixtures, attachLive } = await import("./dist/sim-check.mjs").catch(() => ({}))
  if (checkFixtures) {
    let cases = 0
    const bad = []
    for (const p of attachLive(JSON.parse(fleet)).periods)
      for (const c of p.clusters)
        for (const g of c.nodegroups) {
          if (!g.sim) continue
          cases += g.sim.packFixtures.length
          for (const line of checkFixtures(g.sim)) bad.push(`${c.env}/${g.name}: ${line}`)
        }
    if (bad.length) {
      fail(`packer diverged from the engine on ${bad.length} case(s)`)
      bad.slice(0, 3).forEach((b) => console.error("      " + b))
    } else console.log(`ok    packer    matches the engine on ${cases} case(s)`)
  }
}

// The pod nobody selected. Two nodes; `api` requests 6 GiB and uses 3 on each,
// `be` requests nothing and uses 5 on node b. Today both nodes are 60% full by
// request, above CA's 50%, so nothing moves. Cut `api` to 3.5 GiB and node a
// falls to 35%: Cluster Autoscaler evacuates it onto b -- by requests it fits
// -- and b now holds 3 + 3 + 5 = 11 GiB of real use in 10. No per-container
// check sees it: `api`'s own new request is above its use. The unticked `be`
// is who suffers, and the check must say so by name.
{
  const { simulate } = await import("./dist/sim-check.mjs").catch(() => ({}))
  if (simulate) {
    const G = 1024 ** 3
    const wl = (name, busyMem, qos, containers) => ({
      key: `ns/${name}/Deployment`, ns: "ns", name, kind: "Deployment", peakCpu: 0.4,
      peakMem: busyMem * 1.1, busyCpu: 0.3, busyMem, qos, antiaffinity: false, hpa: null,
      drainBlocked: null, containers,
    })
    const none = { drainBlocked: {}, pdb: {}, replicas: {}, nodeSelector: {}, nodeAffinity: {},
                   tolerations: {}, antiaffinity: [], nodeLabels: {}, nodeTaints: {}, unschedulable: [] }
    const sim = {
      nodesNow: 2, nodeCpu: 8, nodeMem: 10 * G, maxPods: 110, headroom: 0.85, pricePerNode: 100,
      packFixtures: [],
      workloads: [
        wl("api", 3 * G, "Burstable", [{ name: "app", replicas: 2, cpuReq: 1, cpuRec: 0.5,
          memReq: 6 * G, memRec: 3.5 * G, actionable: true, blockers: [] }]),
        wl("be", 5 * G, "BestEffort", [{ name: "worker", replicas: 1, cpuReq: 0, cpuRec: null,
          memReq: 0, memRec: null, actionable: false, blockers: [] }]),
      ],
      pods: [{ w: 0, c: 1, m: 6 * G }, { w: 0, c: 1, m: 6 * G }, { w: 1, c: 0, m: 0 }],
      model: {
        nodes: ["a", "b"], keys: ["ns/api/Deployment", "ns/be/Deployment"],
        placed: [
          { p: "api-1", ns: "ns", n: 0, k: 0, c: 1, m: 6 * G, o: "ReplicaSet" },
          { p: "api-2", ns: "ns", n: 1, k: 0, c: 1, m: 6 * G, o: "ReplicaSet" },
          { p: "be-1", ns: "ns", n: 1, k: 1, c: 0, m: 0, o: "ReplicaSet" },
        ],
        allocCpu: 8, allocMem: 10 * G, dsCpu: 0, dsMem: 0, start: 2, minSize: 0,
        ca: { source: "test", enabled: true, threshold: 0.5, ignoresDaemonsets: false,
              skipsSystemPods: true, skipsLocalStorage: true, bulk: 10 },
        rules: none,
        loads: { "ns/api/Deployment": [0.3, 3 * G], "ns/be/Deployment": [0.2, 5 * G] },
        dsBusy: [0, 0], today: { hotCpu: 0.0625, hotMem: 0.8 }, trend: null, levers: {},
      },
    }
    const quiet = simulate(sim, { containers: {}, floors: {} })
    const cut = simulate(sim, { containers: { "ns/api/Deployment::app": {} }, floors: {} })
    const hit = cut.risks.find((r) => r.subject === "Node load after this change")
    if (quiet.risks.some((r) => r.severity === "unsafe") || quiet.after.nodes !== 2)
      fail(`nothing selected should leave both nodes, with no risk: ${JSON.stringify(quiet.risks)}`)
    else if (cut.after.nodes !== 1)
      fail(`Cluster Autoscaler should remove node a after the cut; model left ${cut.after.nodes}`)
    else if (!hit || hit.severity !== "unsafe" || !hit.detail.includes("ns/be (no request)"))
      fail(`an unselected pod left with no room was not flagged: ${JSON.stringify(cut.risks)}`)
    else console.log("ok    unticked  CA's scale-down squeezes a pod outside the selection, and it is named")
  }
}

// Workloads: a row opens the usage trend for that container. The trend itself
// is fetched per container, so assert the dialog opens and asks for the file
// rather than that the data is present.
const wl = [...window.document.querySelectorAll("button")].find((b) =>
  b.textContent?.trim().startsWith("Workloads"),
)
wl?.click()
await wait(250)
const wlRow = [...window.document.querySelectorAll('tr[role="button"]')][0]
if (!wlRow) fail("no clickable workload row")
else {
  wlRow.click()
  await wait(300)
  const text = window.document.querySelector('[role="dialog"]')?.textContent ?? ""
  if (!text) fail("workload dialog did not open")
  else if (!/Spread across replicas/.test(text)) fail("workload dialog missing the trend controls")
  else if (/missing its series/.test(text)) fail("workload dialog rejected a valid trend file")
  else if (/Unexpected token|not valid JSON/.test(text))
    fail("workload dialog leaked a JSON parse error instead of handling the fallback")
  else if (/older method/.test(text))
    fail("trend files on disk are stale; re-run `rightsizer --refresh trends`")
  else console.log(`ok    workload   ${text.trim().length} chars`)

  // Every replica line must be its own colour. This shipped once as eight
  // identical greys, which is indistinguishable from a chart with one line.
  // Scoped per chart: the dialog holds a CPU chart and a memory chart, and the
  // two legitimately reuse the same eight slots.
  const dialog = window.document.querySelector('[role="dialog"]')
  const charts = [...(dialog?.querySelectorAll("svg") ?? [])]
    .map((svg) =>
      [...svg.querySelectorAll("path[stroke]")]
        .map((n) => n.getAttribute("stroke"))
        .filter((c) => c && c.includes("--series-")),
    )
    .filter((list) => list.length > 1)
  const strokes = charts.flat()
  if (charts.length) {
    const clash = charts.find((list) => new Set(list).size !== list.length)
    if (clash)
      fail(`replica lines reuse a colour: ${clash.length} lines, ${new Set(clash).size} colours`)
    else
      console.log(
        `ok    colours   ${charts.length} chart(s), ${new Set(charts[0]).size} distinct replica hues`,
      )
    const distinct = new Set(strokes)

    // The theme must actually define every slot the chart reaches for, or the
    // stroke resolves to nothing and the line disappears entirely.
    const css = fs.readFileSync(`dist/assets/${cssBundle}`, "utf8")
    const undefinedSlots = [...distinct]
      .map((c) => c.match(/--series-\d+/)?.[0])
      .filter((v) => v && !css.includes(`${v}:`))
    if (undefinedSlots.length)
      fail(`chart uses undefined colour slots: ${undefinedSlots.join(", ")}`)
  }

  // Request and recommendation must be separable. Clamping both to one top edge
  // put them on the same pixel in 130 of 200 charts, which reads as a single
  // line and hides the whole comparison. The first row's thresholds happen to
  // sit inside the plotted range, so several rows are scanned -- a check that
  // only ever sees the easy case is not a check.
  const readThresholds = () =>
    [...(window.document.querySelectorAll('[role="dialog"] svg') ?? [])].map((svg) =>
      [...svg.querySelectorAll("line[stroke-dasharray]")]
        .filter((n) => ["3 3", "9 4"].includes(n.getAttribute("stroke-dasharray") || ""))
        .map((n) => Number(n.getAttribute("y1"))),
    )
  let pairsSeen = 0
  let aboveRangeSeen = 0
  let worst = Infinity
  const scanRows = [...window.document.querySelectorAll('tr[role="button"]')].slice(0, 14)
  for (const row of scanRows) {
    row.click()
    await wait(160)
    for (const ys of readThresholds()) {
      if (ys.length !== 2) continue
      pairsSeen++
      worst = Math.min(worst, Math.abs(ys[0] - ys[1]))
      // Both inside the top strip means the out-of-range path was exercised.
      if (ys.every((y) => y < 40)) aboveRangeSeen++
    }
    window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    )
    await wait(60)
  }
  if (!pairsSeen) fail("no chart drew both a request and a recommendation line")
  else if (!aboveRangeSeen)
    fail("scanned rows never put a threshold above the plotted range; the check is vacuous")
  else if (worst < 6)
    fail(`request and recommendation drawn ${worst.toFixed(1)}px apart`)
  else
    console.log(
      `ok    thresholds ${pairsSeen} pairs, ${aboveRangeSeen} above-range, closest ${worst.toFixed(1)}px`,
    )

  scanRows[0].click()
  await wait(300)

  // Whatever the pixels do, the two numbers are stated in words.
  const caption = [...(window.document.querySelectorAll('[role="dialog"] div') ?? [])]
    .map((el) => (el.textContent || "").trim())
    .find((t) => /^request .*recommended/.test(t) && t.length < 160)
  if (!caption) fail("no request → recommended caption under the chart")
  else console.log(`ok    caption   ${caption.slice(0, 58)}`)

  // Each replica gets its own line in the per-replica mode, and the legend
  // isolates. Both are the reason the mode exists, so both are asserted.
  const countLines = () =>
    [...(window.document.querySelectorAll('[role="dialog"] svg') ?? [])]
      .map((svg) => [...svg.querySelectorAll("path[stroke]")]
        .map((n) => n.getAttribute("stroke"))
        .filter((c) => c && c.includes("--series-")))
      .filter((l) => l.length)

  const perReplica = [...window.document.querySelectorAll('[role="dialog"] button')]
    .find((b) => b.textContent?.trim() === "Each replica")
  if (!perReplica) fail("workload dialog is missing the per-replica mode")
  else if (perReplica.disabled) console.log("skip  replicas  (first row has one replica)")
  else {
    perReplica.click()
    await wait(200)
    const before = countLines()
    if (!before.length || before[0].length < 2)
      fail(`per-replica mode drew ${before[0]?.length ?? 0} lines`)
    if (before.some((l) => new Set(l).size !== l.length))
      fail("per-replica lines reuse a colour")

    // Scoped to the first chart: the dialog holds a CPU chart and a memory
    // chart, each with its own legend and its own isolate state.
    const chart = window.document.querySelector('[role="dialog"] svg')?.parentElement
    const legend = [...(chart?.querySelectorAll("button[aria-pressed]") ?? [])]
    if (legend.length !== before[0].length)
      fail(`legend has ${legend.length} entries for ${before[0].length} lines`)
    legend[0].click()
    await wait(150)
    const isolatedCount = countLines()[0]?.length ?? 0
    if (isolatedCount !== 1)
      fail(`isolating a legend entry left ${isolatedCount} lines drawn`)
    legend[0].click()
    await wait(150)
    const restored = countLines()[0]?.length ?? 0
    if (restored !== before[0].length)
      fail(`clicking again restored ${restored} of ${before[0].length} lines`)
    console.log(`ok    isolate   ${before[0].length} lines → 1 → ${restored}`)
  }
}

if (errors.length) {
  console.error(`\nFAIL  ${errors.length} runtime error(s):`)
  errors.slice(0, 3).forEach((e) => console.error("  " + String(e).slice(0, 400)))
  process.exitCode = 1
} else if (!process.exitCode) {
  console.log("\nall views render, no runtime errors")
}

// A selection with nothing ticked is today's configuration. The repricer packs
// from scratch at the headroom target, and on a tight group that needed more
// nodes than it runs: services-2 read "11 nodes, $610/mo more" for changing
// nothing. Every option with levers: nothing ticked is today on the same node
// type, and everything ticked reproduces the published figure.
{
  const { priceSelection, hpaId, reqId, attachLive } = await import(new URL("./dist/choices-check.mjs", import.meta.url))
  // As the console loads it: each group's model gets its cluster's live objects.
  const f = attachLive(JSON.parse(fleet))
  const empty = { floors: {}, requests: {}, offHpa: [], offReq: [] }
  let roomChecked = 0
  let checked = 0
  for (const c of f.periods[0].clusters) for (const g of c.nodegroups) {
    const asis = (g.scenarios || []).find((s) => s.key === "asis")
    for (const s of g.scenarios || []) {
      if (s.key === "asis") continue
      const req = s.requires || []
      const drivers = req.some((r) => r.includes("container request changes")) ? (g.drivers || []) : []
      const reps = req.some((r) => r.includes("minReplicas")) ? (s.replicaChanges || g.replicaChanges || []) : []
      if (!drivers.length && !reps.length) continue
      // A Karpenter pool's fleet comes from the CLI's Karpenter replay, which
      // the browser does not run; the dialog says so instead of re-pricing.
      if (s.fleet?.length) continue
      const all = priceSelection(g, s, asis, drivers, reps, empty)
      if (!all) continue
      const none = priceSelection(g, s, asis, drivers, reps,
        { ...empty, offHpa: reps.map(hpaId), offReq: drivers.map(reqId) })
      checked++
      if (all.nodes !== s.nodes)
        fail(`${c.env}/${g.name} ${s.key}: everything ticked packs ${all.nodes}, published ${s.nodes}`)
      if (s.instanceType === asis.instanceType && (none.nodes !== asis.nodes || Math.abs(none.deltaUsd) > 0.5))
        fail(`${c.env}/${g.name} ${s.key}: nothing ticked reads ${none.nodes} nodes, ${Math.round(none.deltaUsd)}/mo`)
      // The headroom verdict must agree too, or the dialog warns about the
      // recommendation it is showing. CPU and memory only: disk and network
      // are judged once, on the option.
      const pyTight = Object.keys(s.margin?.over || {}).some((k) => /^(peak|hot)/.test(k))
      const tsTight = (all.outcome.risks || []).some((r) => r.subject === "Headroom after this change")
      if (all.outcome.pressure?.verdict === "ok" && pyTight !== tsTight)
        fail(`${c.env}/${g.name} ${s.key}: headroom is ${pyTight ? "short" : "kept"} in the export, ` +
             `${tsTight ? "short" : "kept"} in the dialog`)
      else if (all.outcome.pressure?.verdict === "ok") roomChecked++
    }
  }
  console.log(`ok    selection  ${checked} option(s): nothing ticked is today, everything ticked is the option; ` +
              `headroom agrees on ${roomChecked}`)

  // A floor is priced on what the HPA runs, not on the floor: 1 core of load
  // on 100m pods runs 15 replicas whatever the floor says -- the same
  // arithmetic as `simulate.running_replicas`.
  {
    const { runningReplicas } = await import(new URL("./dist/sim-check.mjs", import.meta.url))
    const busy = [{ request: 0.1, minutes: { 15: 43200 } }]
    const cases = [
      [runningReplicas(busy, 5, 0.1), 15], [runningReplicas(busy, 20, 0.1), 20],
      [runningReplicas([{ request: 0.1, minutes: { 2: 42900, 3: 300 } }], 2, 0.1), 2],
      [runningReplicas([{ request: 0.2, minutes: { 8: 43200 } }], 5, 0.1), 16],
    ]
    const bad = cases.filter(([got, want]) => got !== want)
    if (bad.length) fail(`runningReplicas disagrees with the Python: ${JSON.stringify(bad)}`)
    else console.log("ok    floors     priced on replicas the HPA runs, not the floor")
  }

  // The instance-type picker judges a type the way the advice does: an option
  // re-priced on its own type is the option -- same nodes, cost, disk,
  // network and headroom verdicts. Otherwise a reader comparing types in the
  // table compares numbers the recommendations never used.
  const { retype } = await import(new URL("./dist/retype-check.mjs", import.meta.url))
  let typed = 0
  for (const c of f.periods[0].clusters) for (const g of c.nodegroups) {
    const asis = (g.scenarios || []).find((s) => s.key === "asis")
    if (!asis || !g.instanceChoices?.types?.length || !g.sim?.model) continue
    for (const s of g.scenarios || []) {
      if (s.key === "asis") continue
      // A Karpenter pool's row is a fleet Karpenter chose; there is no type
      // picker on it and nothing to re-price on "its own type".
      if (s.fleet?.length) continue
      const r = retype(g, s, asis, g.instanceChoices, s.instanceType)
      if (!r) { fail(`${c.env}/${g.name} ${s.key}: cannot be re-priced on its own type ${s.instanceType}`); continue }
      const x = r.scenario
      const same = (a, b) => (a ?? "unknown") === (b ?? "unknown")
      if (x.nodes !== s.nodes || Math.abs(x.deltaUsd - s.deltaUsd) > 1)
        fail(`${c.env}/${g.name} ${s.key}: on its own type the picker reads ${x.nodes} nodes ` +
             `${Math.round(x.deltaUsd)}/mo, the option ${s.nodes} ${Math.round(s.deltaUsd)}/mo`)
      else if (!same(x.storage?.verdict, s.storage?.verdict) || !same(x.network?.verdict, s.network?.verdict))
        fail(`${c.env}/${g.name} ${s.key}: picker disk/network ${x.storage?.verdict}/${x.network?.verdict}, ` +
             `option ${s.storage?.verdict}/${s.network?.verdict}`)
      else if (s.margin && x.margin.ok !== s.margin.ok)
        fail(`${c.env}/${g.name} ${s.key}: picker headroom ${x.margin.ok}, option ${s.margin.ok} ` +
             `(${[...x.margin.notes, ...s.margin.notes].join(" | ")})`)
      else typed++
    }
  }
  console.log(`ok    picker     ${typed} option(s) re-priced on their own type reproduce themselves`)
}
