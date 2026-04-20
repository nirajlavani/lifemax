"use strict";

// Display-only client. Subscribes to /api/stream over SSE, renders state.
// No user input is sent back to the server; mutation happens via Telegram or
// the local CLI bridge.

(function () {
  const els = {
    grid: document.getElementById("grid"),
    stage: document.getElementById("stage"),
    conn: document.getElementById("conn-pill"),
    kanbanChip: document.getElementById("kanban-chip"),
    newsChip: document.getElementById("news-chip"),
    clockTime: document.getElementById("clock-time"),
    clockMeridiem: document.getElementById("clock-meridiem"),
    clockZone: document.getElementById("clock-zone"),
    dateLong: document.getElementById("date-long"),
    dateChip: document.getElementById("date-chip"),
    weatherTemp: document.getElementById("weather-temp"),
    weatherIcon: document.getElementById("weather-icon"),
    weatherLabel: document.getElementById("weather-label"),
    weatherMeta: document.getElementById("weather-meta"),
    slide: document.getElementById("slide"),
    slideImg: document.getElementById("slide-img"),
    slidePlaceholder: document.getElementById("slide-placeholder"),
    slideSource: document.getElementById("slide-source"),
    slideTitle: document.getElementById("slide-title"),
    slideDesc: document.getElementById("slide-desc"),
    slideDots: document.getElementById("slide-dots"),
    slideCounter: document.getElementById("slide-counter"),
    slideProgress: document.getElementById("slide-progress"),
    calChip: document.getElementById("cal-chip"),
    calDow: document.getElementById("cal-dow"),
    calGrid: document.getElementById("cal-grid"),
    calNotice: document.getElementById("cal-notice"),
    dailyChip: document.getElementById("daily-chip"),
    dailyGrid: document.getElementById("daily-grid"),
    dailyEmpty: document.getElementById("daily-empty"),
    // FOCUS · 00 cell removed in BRAND · 12. The kanban already shows the
    // top tier-striped task; we kept `pick_focus` server-side in case we
    // resurface it elsewhere later, but no DOM hooks live for it now.
    ribbon: document.getElementById("ribbon"),
    ribbonList: document.getElementById("ribbon-list"),
    // DISPATCH · 12 — last command cell (top of right column).
    dispatch: document.getElementById("dispatch"),
    dispatchText: document.getElementById("dispatch-text"),
    dispatchVerb: document.getElementById("dispatch-verb"),
    dispatchSubject: document.getElementById("dispatch-subject"),
    dispatchAge: document.getElementById("dispatch-age"),
    dispatchChip: document.getElementById("dispatch-chip"),
    nudgePill: document.getElementById("nudge-pill"),
    nudgeTarget: document.getElementById("nudge-target"),
    // QUOTE · 05 floating ribbon was retired in BRAND · 12 — no DOM hooks.
    timer: document.getElementById("timer"),
    timerPhase: document.getElementById("timer-phase"),
    timerLabel: document.getElementById("timer-label"),
    timerCount: document.getElementById("timer-count"),
    timerMeta: document.getElementById("timer-meta"),
    retro: document.getElementById("q-retro"),
    retroChip: document.getElementById("retro-chip"),
    retroTasks: document.getElementById("retro-tasks"),
    retroHabits: document.getElementById("retro-habits"),
    retroFocus: document.getElementById("retro-focus"),
    retroStrip: document.getElementById("retro-strip"),
    retroCaption: document.getElementById("retro-caption"),
    health: document.getElementById("health"),
    healthRow: document.getElementById("health-row"),
  };

  const SLIDE_INTERVAL_MS = 8000;

  // --- 1600x1066 fit-to-screen scaling --------------------------
  function fitStage() {
    const targetW = 1600;
    const targetH = 1066;
    const sx = window.innerWidth / targetW;
    const sy = window.innerHeight / targetH;
    const scale = Math.min(sx, sy);
    els.grid.style.transform = `scale(${scale})`;
  }
  window.addEventListener("resize", fitStage);
  fitStage();

  // --- Helpers --------------------------------------------------
  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  function safeText(value) {
    if (value === null || value === undefined) return "";
    return String(value);
  }

  function formatDeadline(iso) {
    if (!iso) return null;
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return null;
    const now = new Date();
    const sameDay =
      dt.getFullYear() === now.getFullYear() &&
      dt.getMonth() === now.getMonth() &&
      dt.getDate() === now.getDate();
    let label;
    if (sameDay) {
      label = dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    } else {
      label = dt.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    const overdue = dt.getTime() < now.getTime() && !sameDay;
    return { label, sameDay, overdue };
  }

  function makeTaskNode(task) {
    const node = document.createElement("article");
    let cls = "task";
    if (task.status === "done") cls += " task--done";
    const tier = typeof task.tier === "string" ? task.tier : "none";
    if (tier === "overdue" || tier === "today" || tier === "soon") {
      cls += ` task--tier-${tier}`;
    }
    node.className = cls;

    const title = document.createElement("div");
    title.className = "task__title";
    title.textContent = task.title;
    node.appendChild(title);

    const urg = document.createElement("span");
    urg.className =
      "task__urgency" + (task.urgency === "urgent" ? " task__urgency--urgent" : "");
    urg.title = task.urgency || "";
    node.appendChild(urg);

    const meta = document.createElement("div");
    meta.className = "task__meta";

    const deadline = formatDeadline(task.deadline);
    if (deadline) {
      const pill = document.createElement("span");
      let cls = "task__deadline";
      if (deadline.overdue) cls += " task__deadline--overdue";
      else if (deadline.sameDay) cls += " task__deadline--today";
      pill.className = cls;
      pill.textContent = deadline.label;
      meta.appendChild(pill);
    }

    const prio = document.createElement("span");
    prio.className = "task__priority task__priority--" + safeText(task.priority);
    prio.textContent = safeText(task.priority);
    meta.appendChild(prio);

    if (task.description) {
      const desc = document.createElement("span");
      desc.className = "task__desc";
      desc.textContent = "· " + task.description.slice(0, 80);
      desc.title = task.description;
      meta.appendChild(desc);
    }

    node.appendChild(meta);
    return node;
  }

  function renderEmpty(container, label) {
    container.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = label;
    container.appendChild(empty);
  }

  // --- Renderers ------------------------------------------------
  function renderKanban(tasks, todayCount) {
    const buckets = { todo: [], in_progress: [], done: [] };
    for (const t of tasks) {
      if (buckets[t.status]) buckets[t.status].push(t);
    }
    for (const [status, items] of Object.entries(buckets)) {
      const container = document.querySelector(`[data-cards-for="${status}"]`);
      const count = document.querySelector(`[data-count-for="${status}"]`);
      if (count) count.textContent = String(items.length);
      if (!container) continue;
      if (items.length === 0) {
        renderEmpty(container, "—");
      } else {
        container.innerHTML = "";
        for (const t of items) container.appendChild(makeTaskNode(t));
      }
    }
    setText(els.kanbanChip, `${todayCount} today`);
  }

  function renderClock(clock) {
    if (!clock) return;
    const time = clock.time_12h || clock.time_24h || "--:--";
    const parts = time.split(" ");
    setText(els.clockTime, parts[0] || time);
    setText(els.clockMeridiem, (parts[1] || clock.tz_label || "").toUpperCase());
    setText(els.clockZone, clock.tz_label || "EST");
    setText(els.dateLong, clock.date_long || "—");
  }

  function renderWeather(w) {
    if (!w) return;
    const t = w.temperature_f;
    setText(els.weatherTemp, t === null || t === undefined ? "--°" : `${Math.round(t)}°`);
    setText(els.weatherLabel, w.label || "—");
    const meta = [];
    if (w.city) meta.push([w.city, w.region].filter(Boolean).join(", "));
    if (typeof w.humidity === "number") meta.push(`${Math.round(w.humidity)}% rh`);
    if (typeof w.wind_mph === "number") meta.push(`${Math.round(w.wind_mph)} mph`);
    setText(els.weatherMeta, meta.join(" · ") || "—");
    if (els.weatherIcon) els.weatherIcon.textContent = pickWeatherGlyph(w.code);
  }

  function pickWeatherGlyph(code) {
    if (code === null || code === undefined) return "·";
    if (code === 0 || code === 1) return "☀";
    if (code === 2) return "⛅";
    if (code === 3) return "☁";
    if (code === 45 || code === 48) return "≋";
    if (code >= 51 && code <= 67) return "☂";
    if (code >= 71 && code <= 86) return "❄";
    if (code >= 95) return "⚡";
    return "·";
  }

  function renderToday(todayCount, nudges) {
    const counts = (nudges && nudges.tier_counts) || {};
    const overdue = Number.isFinite(counts.overdue) ? counts.overdue : 0;
    const today = Number.isFinite(counts.today) ? counts.today : todayCount || 0;
    const parts = [];
    if (overdue > 0) parts.push(`${overdue} overdue`);
    if (today > 0) parts.push(`${today} due today`);
    setText(
      els.dateChip,
      parts.length ? parts.join(" · ") : `${today} due today`
    );
  }

  // --- Topbar 'next due' nudge (NEXT · 10) ---------------------
  // Server-rendered tier ('overdue' / 'today' / 'soon' / 'later') drives
  // both the colour token and the optional pulse animation. We never compute
  // tiers on the client to avoid clock-drift mismatches with the kanban.
  function renderNudges(nudges, todayCount) {
    if (!els.nudgePill) return;
    const next = nudges && nudges.next_due;
    if (!next || !next.tier) {
      els.nudgePill.dataset.tier = "later";
      els.nudgePill.className = "nudge nudge--later";
      els.nudgePill.textContent = todayCount > 0 ? "all clear today" : "nothing due";
      els.nudgePill.title = "no upcoming deadlines";
      if (els.nudgeTarget) {
        els.nudgeTarget.hidden = true;
        els.nudgeTarget.textContent = "";
      }
      return;
    }
    const tier = next.tier;
    els.nudgePill.dataset.tier = tier;
    els.nudgePill.className = `nudge nudge--${tier}`;
    els.nudgePill.textContent = next.countdown_label || tier;
    const target = next.title || "";
    els.nudgePill.title = target ? `${target} · ${next.countdown_label || ""}`.trim() : tier;
    if (els.nudgeTarget) {
      if (target) {
        els.nudgeTarget.hidden = false;
        els.nudgeTarget.textContent = target;
      } else {
        els.nudgeTarget.hidden = true;
        els.nudgeTarget.textContent = "";
      }
    }
  }

  function focusToneFromReason(reason) {
    if (!reason) return "later";
    if (reason.startsWith("overdue")) return "overdue";
    if (reason.startsWith("due today") || reason === "urgent") return "today";
    if (reason.startsWith("soon")) return "soon";
    if (reason.startsWith("in progress")) return "today";
    return "later";
  }

  // BRAND · 12 — `renderFocus` is intentionally a no-op now. The dedicated
  // FOCUS · 00 quadrant was removed; the kanban already surfaces the next
  // actionable task via the tier stripe on its top card. We keep the
  // function name so any stray call (or future re-introduction) is safe.
  function renderFocus(_focus) {
    return;
  }

  // --- AI feed slideshow ---------------------------------------
  // BRAND · 11 fix — `applySnapshot` runs on every SSE tick (~5s). The
  // slide interval is 8s, so re-rendering on every snapshot was resetting
  // the progress bar before it ever reached 100%, and the slide never
  // visibly advanced. We now hash the active item list and short-circuit
  // when nothing changed, leaving the timer + bar untouched.
  const slideshow = {
    items: [],
    idx: 0,
    timer: null,
    progressTimer: null,
    currentLink: null,
    signature: "",
  };

  function _newsSignature(items) {
    if (!items || !items.length) return "0|";
    // Link is unique per article and stable across refreshes.
    const links = [];
    for (const it of items) {
      links.push((it && it.link) || "");
    }
    return `${items.length}|${links.join("\u0001")}`;
  }

  function hostnameFromLink(link) {
    try {
      return new URL(link).hostname.replace(/^www\./, "");
    } catch (_) {
      return "";
    }
  }

  function placeholderGlyph(it) {
    const src = (it && (it.source || hostnameFromLink(it.link))) || "·";
    const ch = src.trim().charAt(0);
    return ch ? ch.toUpperCase() : "·";
  }

  function renderDots(count, activeIdx) {
    if (!els.slideDots) return;
    if (els.slideDots.childElementCount !== count) {
      els.slideDots.innerHTML = "";
      for (let i = 0; i < count; i += 1) {
        const d = document.createElement("span");
        d.className = "slide__dot";
        els.slideDots.appendChild(d);
      }
    }
    const dots = els.slideDots.children;
    for (let i = 0; i < dots.length; i += 1) {
      dots[i].className = "slide__dot" + (i === activeIdx ? " slide__dot--active" : "");
    }
  }

  function showSlide(idx) {
    const items = slideshow.items;
    if (!items.length) {
      slideshow.idx = 0;
      slideshow.currentLink = null;
      els.slide.dataset.state = "empty";
      els.slide.removeAttribute("data-has-image");
      setText(els.slideSource, "—");
      setText(els.slideTitle, "awaiting signal");
      setText(els.slideDesc, "");
      els.slidePlaceholder.textContent = "·";
      setText(els.slideCounter, "0 / 0");
      renderDots(0, -1);
      resetProgressBar();
      return;
    }
    const safeIdx = ((idx % items.length) + items.length) % items.length;
    slideshow.idx = safeIdx;
    const it = items[safeIdx];
    slideshow.currentLink = it.link || null;
    els.slide.dataset.state = "loaded";

    setText(els.slideSource, it.source || hostnameFromLink(it.link) || "—");
    setText(els.slideTitle, it.title || "—");
    setText(els.slideDesc, it.description || "");
    els.slidePlaceholder.textContent = placeholderGlyph(it);
    setText(els.slideCounter, `${safeIdx + 1} / ${items.length}`);
    renderDots(items.length, safeIdx);

    if (it.image) {
      const url = it.image;
      els.slideImg.onload = () => {
        if (els.slideImg.getAttribute("src") === url) {
          els.slide.setAttribute("data-has-image", "true");
        }
      };
      els.slideImg.onerror = () => {
        if (els.slideImg.getAttribute("src") === url) {
          els.slide.removeAttribute("data-has-image");
          els.slideImg.removeAttribute("src");
        }
      };
      els.slide.removeAttribute("data-has-image");
      els.slideImg.alt = it.title || "";
      els.slideImg.src = url;
    } else {
      els.slide.removeAttribute("data-has-image");
      els.slideImg.removeAttribute("src");
      els.slideImg.alt = "";
    }

    restartProgressBar();
  }

  function resetProgressBar() {
    if (!els.slideProgress) return;
    els.slideProgress.style.transition = "none";
    els.slideProgress.style.width = "0%";
  }

  function restartProgressBar() {
    if (!els.slideProgress) return;
    resetProgressBar();
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        els.slideProgress.style.transition = `width ${SLIDE_INTERVAL_MS}ms linear`;
        els.slideProgress.style.width = "100%";
      });
    });
  }

  function startSlideshowTimer() {
    if (slideshow.timer) clearInterval(slideshow.timer);
    if (slideshow.items.length < 2) {
      slideshow.timer = null;
      return;
    }
    slideshow.timer = setInterval(() => {
      showSlide(slideshow.idx + 1);
    }, SLIDE_INTERVAL_MS);
  }

  function renderNews(items) {
    if (!items) items = [];
    setText(els.newsChip, `${items.length} items`);

    const sig = _newsSignature(items);

    // Same item list as last tick → don't disturb the active slide.
    // We still keep `slideshow.items` pointed at the new array reference
    // (in case object identity matters elsewhere), but skip showSlide/timer
    // so the progress bar can finish its 8s sweep and advance naturally.
    if (sig === slideshow.signature && slideshow.items.length === items.length) {
      slideshow.items = items;
      return;
    }

    const prevLink = slideshow.currentLink;
    slideshow.items = items;
    slideshow.signature = sig;

    let nextIdx = 0;
    if (prevLink) {
      const found = items.findIndex((it) => it && it.link === prevLink);
      if (found >= 0) nextIdx = found;
    }
    showSlide(nextIdx);
    startSlideshowTimer();
  }

  // --- 2-week calendar -----------------------------------------
  // Bucket tasks by their deadline date in the dashboard's local TZ (which the
  // server already echoes via snap.clock.iso). The grid spans today + 13 days.
  const CAL_DAYS = 14;
  const DOW_LABELS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
  const MON_LABELS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];

  function localDateKey(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function localDayStart(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function parseLocalToday(clock) {
    // clock.iso looks like "2026-04-18T17:49:29.547484-04:00". Browsers in
    // any timezone parse this correctly, then we drop the time portion.
    if (clock && clock.iso) {
      const dt = new Date(clock.iso);
      if (!Number.isNaN(dt.getTime())) return localDayStart(dt);
    }
    return localDayStart(new Date());
  }

  function bucketTasksByDay(tasks) {
    const buckets = new Map();
    for (const t of tasks) {
      if (!t || !t.deadline) continue;
      const dt = new Date(t.deadline);
      if (Number.isNaN(dt.getTime())) continue;
      const key = localDateKey(dt);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(t);
    }
    for (const list of buckets.values()) {
      list.sort((a, b) => {
        const ap = priorityRank(a.priority);
        const bp = priorityRank(b.priority);
        if (ap !== bp) return ap - bp;
        return new Date(a.deadline) - new Date(b.deadline);
      });
    }
    return buckets;
  }

  function bucketEventsByDay(events) {
    // Expand multi-day events so they appear on every day they touch.
    const buckets = new Map();
    for (const ev of events || []) {
      if (!ev || !ev.start) continue;
      const startDt = new Date(ev.start);
      const endDt = ev.end ? new Date(ev.end) : new Date(startDt);
      if (Number.isNaN(startDt.getTime()) || Number.isNaN(endDt.getTime())) continue;
      let cursor = localDayStart(startDt);
      const lastDay = localDayStart(endDt);
      let safety = 0;
      while (cursor <= lastDay && safety < 60) {
        const key = localDateKey(cursor);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(ev);
        cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1);
        safety += 1;
      }
    }
    for (const list of buckets.values()) {
      list.sort((a, b) => new Date(a.start) - new Date(b.start));
    }
    return buckets;
  }

  function formatEventTime(ev) {
    if (!ev) return "";
    if (ev.all_day) return "all day";
    try {
      const dt = new Date(ev.start);
      if (Number.isNaN(dt.getTime())) return "";
      return dt
        .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
        .toLowerCase();
    } catch (_) {
      return "";
    }
  }

  function priorityRank(p) {
    if (p === "high") return 0;
    if (p === "medium") return 1;
    return 2;
  }

  function ensureDowHeader(startDayIdx) {
    if (!els.calDow) return;
    if (els.calDow.childElementCount === 7 && els.calDow.dataset.start === String(startDayIdx)) return;
    els.calDow.dataset.start = String(startDayIdx);
    els.calDow.innerHTML = "";
    for (let i = 0; i < 7; i += 1) {
      const span = document.createElement("span");
      span.textContent = DOW_LABELS[(startDayIdx + i) % 7];
      els.calDow.appendChild(span);
    }
  }

  function renderCalendar(tasks, events, clock) {
    if (!els.calGrid) return;
    const today = parseLocalToday(clock);
    const taskBuckets = bucketTasksByDay(tasks || []);
    const eventBuckets = bucketEventsByDay(events || []);

    ensureDowHeader(today.getDay());
    els.calGrid.innerHTML = "";

    let openTaskCount = 0;
    let totalEventCount = 0;
    const todayKey = localDateKey(today);

    for (let i = 0; i < CAL_DAYS; i += 1) {
      const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
      const key = localDateKey(d);
      const tItems = taskBuckets.get(key) || [];
      const eItems = eventBuckets.get(key) || [];
      openTaskCount += tItems.filter((t) => t.status !== "done" && !t.archived).length;
      totalEventCount += eItems.length;

      const cell = document.createElement("div");
      const classes = ["cal__day"];
      if (key === todayKey) classes.push("cal__day--today");
      const dow = d.getDay();
      if (dow === 0 || dow === 6) classes.push("cal__day--weekend");
      cell.className = classes.join(" ");

      const head = document.createElement("div");
      head.className = "cal__day-head";

      const num = document.createElement("span");
      num.className = "cal__num";
      num.textContent = String(d.getDate());
      head.appendChild(num);

      // Show month label only on the 1st of each month (and on the very first cell)
      if (i === 0 || d.getDate() === 1) {
        const mon = document.createElement("span");
        mon.className = "cal__mon";
        mon.textContent = MON_LABELS[d.getMonth()];
        head.appendChild(mon);
      }

      cell.appendChild(head);

      const totalItems = tItems.length + eItems.length;
      if (totalItems > 0) {
        const badge = document.createElement("span");
        badge.className = "cal__badge";
        badge.textContent = String(totalItems);
        cell.appendChild(badge);

        const pills = document.createElement("div");
        pills.className = "cal__pills";

        // Events first (they're time-anchored, not just "due-by"), then tasks.
        const eventVisible = eItems.slice(0, 2);
        for (const ev of eventVisible) {
          const pill = document.createElement("div");
          pill.className = "cal__pill cal__pill--event";
          if (ev.is_lifemax) pill.classList.add("cal__pill--event-lifemax");
          // Color stripe from the source calendar when available.
          if (ev.calendar_color) {
            pill.style.borderLeftColor = ev.calendar_color;
          }
          const time = formatEventTime(ev);
          const titleSpan = document.createElement("span");
          titleSpan.className = "cal__pill-title";
          titleSpan.textContent = ev.title || "(untitled)";
          pill.appendChild(titleSpan);
          if (time) {
            const t = document.createElement("span");
            t.className = "cal__pill-time";
            t.textContent = time;
            pill.appendChild(t);
          }
          const tip = [
            ev.title,
            time ? `· ${time}` : "",
            ev.calendar ? `(${ev.calendar})` : "",
          ]
            .filter(Boolean)
            .join(" ");
          pill.title = tip;
          pills.appendChild(pill);
        }

        const taskBudget = Math.max(0, 2 - eventVisible.length);
        const taskVisible = tItems.slice(0, taskBudget);
        for (const t of taskVisible) {
          const pill = document.createElement("div");
          let cls = "cal__pill";
          if (t.priority === "high") cls += " cal__pill--high";
          else if (t.priority === "medium") cls += " cal__pill--medium";
          else if (t.priority === "low") cls += " cal__pill--low";
          if (t.status === "done") cls += " cal__pill--done";
          pill.className = cls;
          pill.textContent = t.title;
          pill.title = t.title;
          pills.appendChild(pill);
        }

        const shownCount = eventVisible.length + taskVisible.length;
        if (totalItems > shownCount) {
          const more = document.createElement("div");
          more.className = "cal__pill-more";
          more.textContent = `+${totalItems - shownCount} more`;
          pills.appendChild(more);
        }

        cell.appendChild(pills);
      }

      els.calGrid.appendChild(cell);
    }

    const chipText =
      totalEventCount > 0
        ? `${openTaskCount} task${openTaskCount === 1 ? "" : "s"} · ${totalEventCount} event${totalEventCount === 1 ? "" : "s"}`
        : `${openTaskCount} scheduled`;
    setText(els.calChip, chipText);
  }

  function renderCalendarNotice(status) {
    if (!els.calNotice) return;
    if (!status || status.error == null) {
      els.calNotice.hidden = true;
      els.calNotice.textContent = "";
      return;
    }
    els.calNotice.hidden = false;
    els.calNotice.textContent = status.error;
  }

  // --- Daily checklist -----------------------------------------
  // Items reset every "habit day" — local midnight, with a 3am cutoff so
  // late-night check-offs still belong to the previous day. This MUST match
  // `habit_day_in_tz` on the server (cutoff comes through in snap.habits).
  function localHabitDateKey(now, cutoffHour) {
    const cutoff = Number.isInteger(cutoffHour) ? cutoffHour : 3;
    let d = new Date(now.getTime());
    if (d.getHours() < cutoff) {
      d = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1);
    }
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function renderDaily(habits, clock) {
    if (!els.dailyGrid) return;
    const payload = habits || { items: [], cutoff_hour: 3 };
    const items = payload.items || [];

    // Re-derive done_today on the client too: if the page sat across the 3am
    // boundary we may not yet have a fresh snapshot, so trust the wall clock.
    const now = (clock && clock.iso) ? new Date(clock.iso) : new Date();
    const todayKey = (typeof payload.today_local_date === "string"
      ? payload.today_local_date
      : localHabitDateKey(now, payload.cutoff_hour));

    let doneCount = 0;
    els.dailyGrid.innerHTML = "";
    for (const item of items) {
      if (!item) continue;
      const isDone = item.last_done_local_date === todayKey;
      if (isDone) doneCount += 1;

      const row = document.createElement("div");
      row.className = "daily__item" + (isDone ? " daily__item--done" : "");
      row.dataset.habitId = item.id;

      const streakNum = Number.isFinite(item.current_streak) ? item.current_streak : 0;
      const bestNum = Number.isFinite(item.best_streak) ? item.best_streak : streakNum;
      row.title = isDone
        ? `done today (${todayKey}) · ${streakNum}d streak · best ${bestNum}d`
        : `not yet today · ${streakNum}d streak · best ${bestNum}d`;

      const check = document.createElement("span");
      check.className = "daily__check";
      check.setAttribute("aria-hidden", "true");
      row.appendChild(check);

      const title = document.createElement("span");
      title.className = "daily__title";
      title.textContent = item.title;
      row.appendChild(title);

      const streak = document.createElement("span");
      let streakCls = "daily__streak";
      if (streakNum >= 7) streakCls += " daily__streak--big";
      else if (streakNum > 0) streakCls += " daily__streak--alive";
      streak.className = streakCls;
      const flame = document.createElement("span");
      flame.className = "daily__streak-flame";
      flame.textContent = streakNum > 0 ? "↑" : "·";
      streak.appendChild(flame);
      const num = document.createElement("span");
      num.textContent = `${streakNum}d`;
      streak.appendChild(num);
      row.appendChild(streak);

      const history = Array.isArray(item.done_last_7) ? item.done_last_7 : [];
      if (history.length === 7) {
        const strip = document.createElement("div");
        strip.className = "daily__history";
        for (let i = 0; i < 7; i += 1) {
          const cell = document.createElement("span");
          let cls = "daily__history-cell";
          if (history[i]) cls += " daily__history-cell--done";
          if (i === 6) cls += " daily__history-cell--today";
          cell.className = cls;
          strip.appendChild(cell);
        }
        row.appendChild(strip);
      }

      els.dailyGrid.appendChild(row);
    }

    const total = items.length;
    if (els.dailyEmpty) els.dailyEmpty.hidden = total > 0;
    const top = Number.isFinite(payload.top_streak) ? payload.top_streak : 0;
    const chip = top > 1
      ? `${doneCount} / ${total} · top ${top}d`
      : `${doneCount} / ${total}`;
    setText(els.dailyChip, chip);
  }

  // --- Dispatch history ribbon ---------------------------------
  // Reflects the last few ./bin/lifemax / Telegram dispatches as a thin
  // strip in the bottom-left corner. Display-only (no clicks).
  const RIBBON_VERBS = {
    create: "added",
    update: "updated",
    complete: "completed",
    archive: "archived",
    add_event: "scheduled",
    add_habit: "added daily",
    check_habit: "checked",
    uncheck_habit: "unchecked",
    remove_habit: "removed daily",
    query: "asked",
    undo: "undone",
  };

  function ribbonAge(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    if (seconds < 5) return "now";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  // DISPATCH · 12 — surface the most recent Telegram / CLI prompt in its
  // own quadrant. We deliberately reuse `snap.history.items` (already
  // plumbed end-to-end with redaction + truncation in
  // `dispatch_history.py`) so this widget adds zero new server-side state.
  function renderLastCommand(history) {
    if (!els.dispatch || !els.dispatchText) return;
    const items = history && Array.isArray(history.items) ? history.items : [];
    const top = items.length > 0 ? items[0] : null;
    const cell = els.dispatch;
    cell.classList.remove(
      "dispatch--ok",
      "dispatch--err",
      "dispatch--undo",
    );
    if (els.dispatchChip) {
      els.dispatchChip.classList.remove(
        "dispatch__chip--ok",
        "dispatch__chip--err",
        "dispatch__chip--undo",
      );
    }
    if (!top) {
      cell.dataset.state = "empty";
      setText(els.dispatchText, "no prompts yet");
      setText(els.dispatchVerb, "—");
      setText(els.dispatchSubject, "awaiting input");
      setText(els.dispatchAge, "—");
      setText(els.dispatchChip, "idle");
      return;
    }
    cell.dataset.state = "filled";

    // Outcome state — drives the chip dot + the verb colour. Mirrors the
    // ribbon's pip semantics so the two widgets read the same way.
    let stateCls;
    let chipCls;
    let chipText;
    if (top.undid) {
      stateCls = "dispatch--undo";
      chipCls = "dispatch__chip--undo";
      chipText = "undone";
    } else if (top.ok) {
      stateCls = "dispatch--ok";
      chipCls = "dispatch__chip--ok";
      chipText = "ok";
    } else {
      stateCls = "dispatch--err";
      chipCls = "dispatch__chip--err";
      chipText = "failed";
    }
    cell.classList.add(stateCls);

    const rawText = (typeof top.input_text === "string" && top.input_text.trim())
      ? top.input_text.trim()
      : "(empty prompt)";
    setText(els.dispatchText, rawText);

    const verb = top.undid
      ? "undid"
      : (RIBBON_VERBS[top.action] || top.action || "—");
    setText(els.dispatchVerb, verb);

    const subjectText = (typeof top.subject === "string" && top.subject.trim())
      ? top.subject.trim()
      : (typeof top.message === "string" && top.message.trim()
          ? top.message.trim()
          : "no subject");
    setText(els.dispatchSubject, subjectText);

    setText(els.dispatchAge, ribbonAge(top.age_seconds));

    if (els.dispatchChip) {
      els.dispatchChip.classList.add(chipCls);
      setText(els.dispatchChip, chipText);
    }
  }

  function renderRibbon(history) {
    if (!els.ribbon || !els.ribbonList) return;
    const items = history && Array.isArray(history.items) ? history.items : [];
    if (items.length === 0) {
      els.ribbon.hidden = true;
      els.ribbonList.innerHTML = "";
      return;
    }
    const visible = items.slice(0, 4);
    els.ribbon.hidden = false;
    els.ribbonList.innerHTML = "";

    let nextUndoTitle = null;

    for (const it of visible) {
      const li = document.createElement("li");
      li.className = "ribbon__item";

      const pip = document.createElement("span");
      let pipCls = "ribbon__pip";
      if (it.undid) pipCls += " ribbon__pip--undo";
      else if (it.ok) pipCls += " ribbon__pip--ok";
      else pipCls += " ribbon__pip--err";
      pip.className = pipCls;
      li.appendChild(pip);

      const verb = document.createElement("span");
      let verbCls = "ribbon__verb";
      if (it.undid) verbCls += " ribbon__verb--undo";
      else if (it.ok) verbCls += " ribbon__verb--ok";
      else verbCls += " ribbon__verb--err";
      verb.className = verbCls;
      verb.textContent = it.undid
        ? "undid"
        : (RIBBON_VERBS[it.action] || it.action || "—");
      li.appendChild(verb);

      const subj = document.createElement("span");
      subj.className = "ribbon__subject";
      const subjectText = (typeof it.subject === "string" && it.subject.trim())
        ? it.subject.trim()
        : (typeof it.input_text === "string" ? it.input_text.trim() : "");
      subj.textContent = subjectText || "—";
      subj.title = it.message || subjectText || "";
      li.appendChild(subj);

      const age = document.createElement("span");
      age.className = "ribbon__age";
      age.textContent = ribbonAge(it.age_seconds);
      li.appendChild(age);

      els.ribbonList.appendChild(li);

      if (!nextUndoTitle && it.ok && !it.undid && it.undo_payload) {
        nextUndoTitle = subjectText || RIBBON_VERBS[it.action] || "this";
      }
    }

    if (nextUndoTitle) {
      const hint = document.createElement("li");
      hint.className = "ribbon__item";
      const tag = document.createElement("span");
      tag.className = "ribbon__hint";
      tag.textContent = "say 'undo' →";
      tag.title = `undo: ${nextUndoTitle}`;
      hint.appendChild(tag);
      els.ribbonList.appendChild(hint);
    }
  }

  // BRAND · 12 — `renderQuote` retired with the QUOTE · 05 ribbon. The
  // server still produces `snap.quote`; we just stopped consuming it.
  // Kept as a no-op so any stray call is safe.
  function renderQuote(_quote) {
    return;
  }

  // ---- Focus timer band (FOCUS · 06) -----------------------------------
  // The server is the single source of truth for timer state: every snapshot
  // includes a `timer` object with `state`, `phase`, `remaining_seconds`, and
  // `ends_at`. We also run a local 1-second loop so the displayed countdown
  // ticks down smoothly between SSE updates. The local loop snaps back to
  // server time as soon as a fresh snapshot lands.
  const _timer = {
    last: null,
    serverEndsAtMs: null,
    lastSequence: -1,
    intervalId: null,
    audioCtx: null,
  };

  function _formatCountdown(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function _phaseLabel(phase) {
    if (phase === "focus") return "FOCUS";
    if (phase === "break_short") return "BREAK";
    if (phase === "break_long") return "LONG";
    return (phase || "").toUpperCase();
  }

  function _playChime() {
    try {
      if (!_timer.audioCtx) {
        const Ctor = window.AudioContext || window.webkitAudioContext;
        if (!Ctor) return;
        _timer.audioCtx = new Ctor();
      }
      const ctx = _timer.audioCtx;
      const now = ctx.currentTime;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, now);
      osc.frequency.exponentialRampToValueAtTime(660, now + 0.18);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.18, now + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.45);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.5);
    } catch (_e) {
      // Ignore — autoplay restrictions keep this silent until first input.
    }
  }

  function _redrawTimer() {
    const t = _timer.last;
    if (!t || !els.timer) return;
    if (t.state === "running" || t.state === "break") {
      let remaining = t.remaining_seconds || 0;
      if (_timer.serverEndsAtMs) {
        remaining = Math.max(0, (_timer.serverEndsAtMs - Date.now()) / 1000);
      }
      setText(els.timerCount, _formatCountdown(remaining));
    } else if (t.state === "paused") {
      setText(els.timerCount, _formatCountdown(t.remaining_seconds || 0));
    }
  }

  function _ensureTicker() {
    if (_timer.intervalId !== null) return;
    _timer.intervalId = window.setInterval(_redrawTimer, 1000);
  }
  function _stopTicker() {
    if (_timer.intervalId === null) return;
    window.clearInterval(_timer.intervalId);
    _timer.intervalId = null;
  }

  function renderTimer(timer) {
    if (!els.timer) return;
    if (!timer || timer.state === "idle") {
      els.timer.hidden = true;
      els.timer.classList.remove("timer--paused", "timer--break");
      _timer.last = null;
      _timer.serverEndsAtMs = null;
      _stopTicker();
      return;
    }

    const phase = safeText(timer.phase) || "focus";
    els.timer.hidden = false;
    els.timer.classList.toggle("timer--paused", timer.state === "paused");
    els.timer.classList.toggle("timer--break", timer.state === "break");
    if (els.timerPhase) {
      els.timerPhase.classList.toggle("timer__phase--break", phase !== "focus");
      setText(els.timerPhase, _phaseLabel(phase));
    }
    if (els.timerLabel) {
      setText(els.timerLabel, safeText(timer.label) || "");
    }
    if (els.timerMeta) {
      const completed = Number(timer.completed_focus_blocks_today || 0);
      let meta;
      if (timer.state === "running") meta = "running";
      else if (timer.state === "paused") meta = "paused";
      else if (timer.state === "break") meta = "break";
      else meta = String(timer.state || "");
      if (completed > 0) meta = `${meta} · ${completed} focus today`;
      setText(els.timerMeta, meta);
    }

    if (timer.ends_at && (timer.state === "running" || timer.state === "break")) {
      const ms = Date.parse(timer.ends_at);
      _timer.serverEndsAtMs = Number.isNaN(ms) ? null : ms;
    } else {
      _timer.serverEndsAtMs = null;
    }
    _timer.last = timer;
    setText(els.timerCount, _formatCountdown(timer.remaining_seconds || 0));

    if (timer.state === "running" || timer.state === "break") _ensureTicker();
    else _stopTicker();

    // One-shot chime when the server tells us a phase just elapsed.
    const seq = Number(timer.sequence || 0);
    if (timer.last_event === "elapsed" && seq !== _timer.lastSequence) {
      _timer.lastSequence = seq;
      _playChime();
    } else if (seq !== _timer.lastSequence) {
      _timer.lastSequence = seq;
    }
  }

  // ---- Weekly retro card (RETRO · 07) ----------------------------------
  // BRAND · 11 update — retro is a real quadrant in the right column now,
  // not a floating overlay. We toggle `#q-retro[hidden]` directly; the
  // right-column grid uses :has() to reflow the feed + checklist when the
  // retro is hidden (any non-Sunday).
  const _DOW_LABELS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

  function _formatDowFromIso(iso) {
    if (!iso) return "";
    const dt = new Date(`${iso}T12:00:00`);
    if (Number.isNaN(dt.getTime())) return "";
    // Date#getDay() returns 0=Sun..6=Sat. Map onto our Mon-first label list.
    const idx = (dt.getDay() + 6) % 7;
    return _DOW_LABELS[idx];
  }

  function renderRetro(retro) {
    if (!els.retro) return;
    if (!retro || retro.is_sunday !== true) {
      els.retro.hidden = true;
      return;
    }
    els.retro.hidden = false;

    const tasks = retro.tasks || {};
    const habits = retro.habits || {};
    const focus = retro.focus || {};

    setText(els.retroTasks, String(tasks.completed || 0));
    const ratePct = Math.round((habits.completion_rate || 0) * 100);
    setText(els.retroHabits, `${ratePct}%`);
    setText(els.retroFocus, String(focus.blocks_total || 0));
    if (els.retroChip) {
      const start = retro.window_start || "";
      const end = retro.window_end || retro.today_local_date || "";
      const chip = start && end
        ? `${start.slice(5)} → ${end.slice(5)}`
        : "this week";
      setText(els.retroChip, chip);
    }

    const daily = Array.isArray(retro.daily) ? retro.daily : [];
    const todayIso = retro.today_local_date || "";
    // Find the max combined activity across the 7 days so bar heights are
    // relative — this keeps a single great day from saturating the strip.
    let peak = 0;
    for (const d of daily) {
      const total = (d.tasks_done || 0) + (d.habits_done || 0) + (d.focus_blocks || 0);
      if (total > peak) peak = total;
    }
    const strip = els.retroStrip;
    if (strip) {
      strip.replaceChildren();
      for (const d of daily) {
        const total = (d.tasks_done || 0) + (d.habits_done || 0) + (d.focus_blocks || 0);
        const pct = peak > 0 ? Math.round((total / peak) * 100) : 0;
        const bar = document.createElement("div");
        bar.className = "retro__bar";
        if (d.date === todayIso) bar.classList.add("retro__bar--today");
        const fill = document.createElement("div");
        fill.className = "retro__bar-fill";
        fill.style.setProperty("--retro-fill", `${pct}%`);
        fill.title = `${d.date}: ${total} (${d.tasks_done}t · ${d.habits_done}h · ${d.focus_blocks}f)`;
        const label = document.createElement("div");
        label.className = "retro__bar-label";
        setText(label, _formatDowFromIso(d.date));
        bar.append(fill, label);
        strip.append(bar);
      }
    }

    // Caption picks the most concrete brag the data supports.
    let caption = "no notable runs this week";
    const titles = Array.isArray(tasks.completed_titles) ? tasks.completed_titles : [];
    if (focus.blocks_total > 0 && focus.best_day) {
      caption = `${focus.blocks_total} focus blocks · best day ${_formatDowFromIso(focus.best_day)}`;
    } else if (titles.length > 0) {
      caption = `last shipped · ${titles[0]}`;
    } else if (habits.top_habit && habits.top_habit_count > 0) {
      caption = `${habits.top_habit} ${habits.top_habit_count}× this week`;
    } else if ((tasks.completed || 0) > 0) {
      caption = `${tasks.completed} tasks shipped this week`;
    }
    setText(els.retroCaption, caption);
  }

  // ---- Health vitals badge strip (VITALS · 08) ------------------------
  // Each badge is a tiny <li> with a colored dot + short label. The whole
  // strip is hidden until at least one badge is reported. Worst-tier wins
  // the strip's outline tint so a degraded/down state is visible at a
  // glance without reading text.
  function _humanAge(seconds) {
    if (typeof seconds !== "number" || seconds < 0) return "";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  }

  function renderHealth(health) {
    if (!els.health || !els.healthRow) return;
    if (!health || !Array.isArray(health.badges) || health.badges.length === 0) {
      els.health.hidden = true;
      return;
    }
    els.health.hidden = false;

    // Wash the strip border with the worst-tier accent so the badge speaks
    // even from across the room.
    els.health.classList.remove("health--down", "health--degraded");
    if (health.tier === "down") els.health.classList.add("health--down");
    else if (health.tier === "degraded") els.health.classList.add("health--degraded");

    els.healthRow.replaceChildren();
    for (const badge of health.badges) {
      const tier = badge.tier || "unknown";
      const li = document.createElement("li");
      li.className = `health__badge health__badge--${tier}`;
      const dot = document.createElement("span");
      dot.className = "health__dot";
      dot.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "health__label";
      setText(label, String(badge.label || badge.key || "—").toLowerCase());
      li.append(dot, label);
      // Tooltip stays in the DOM only as plain text → no XSS surface.
      const ageBit = badge.age_seconds != null ? ` · ${_humanAge(badge.age_seconds)}` : "";
      li.title = `${badge.label || badge.key}: ${badge.tier} — ${badge.message || "—"}${ageBit}`;
      els.healthRow.append(li);
    }
  }

  function applySnapshot(snap) {
    if (!snap) return;
    const tasks = snap.tasks || [];
    const todayCount = snap.today_count || 0;
    renderKanban(tasks, todayCount);
    renderCalendar(tasks, snap.events || [], snap.clock);
    renderCalendarNotice(snap.calendar_status);
    renderClock(snap.clock);
    renderWeather(snap.weather);
    renderToday(todayCount, snap.nudges);
    renderNudges(snap.nudges, todayCount);
    renderNews(snap.news);
    renderDaily(snap.habits, snap.clock);
    renderLastCommand(snap.history);
    renderRibbon(snap.history);
    renderTimer(snap.timer);
    renderRetro(snap.retro);
    renderHealth(snap.health);
  }

  // --- Connection management -----------------------------------
  function setConn(state) {
    if (!els.conn) return;
    if (state === "on") {
      els.conn.classList.add("conn-pill--on");
      els.conn.classList.remove("conn-pill--off");
      els.conn.textContent = "live";
    } else {
      els.conn.classList.add("conn-pill--off");
      els.conn.classList.remove("conn-pill--on");
      els.conn.textContent = "offline";
    }
  }

  let es = null;
  let backoff = 1000;

  function connect() {
    try {
      es = new EventSource("/api/stream");
    } catch (err) {
      setConn("off");
      scheduleReconnect();
      return;
    }
    es.addEventListener("open", () => {
      setConn("on");
      backoff = 1000;
    });
    es.addEventListener("snapshot", (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        applySnapshot(snap);
      } catch (err) {
        // Ignore malformed payloads.
      }
    });
    es.addEventListener("ping", () => {
      // keepalive
    });
    es.addEventListener("error", () => {
      setConn("off");
      try {
        es.close();
      } catch (_) {
        // ignore
      }
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    const delay = Math.min(backoff, 15000);
    backoff = Math.min(backoff * 2, 15000);
    setTimeout(connect, delay);
  }

  // Fall back to /api/state on first paint in case SSE is briefly unavailable.
  fetch("/api/state")
    .then((r) => (r.ok ? r.json() : null))
    .then((snap) => snap && applySnapshot(snap))
    .catch(() => {})
    .finally(connect);
})();
