(() => {
  "use strict";

  const boot = window.__BOOT__ || { user: null, matches: [], error: null, dev: false };
  const tg = (window.Telegram && window.Telegram.WebApp) || window.__TG_SHIM__ || null;
  const hasTelegram = !!(tg && tg.initData);

  const state = {
    user: boot.user || null,
    matches: boot.matches || [],
    liveMatches: boot.live || [],   // матчи в эфире (state.live — флаг «данные загружены»)
    segment: "line",                // «Линия» / «Live» внутри вкладки матчей
    bets: [],
    expresses: [],
    limits: { min_bet: 10, max_legs: 6, max_odds: 40, daily_bonus: 250 },
    cart: readCart(),
    tab: "matches",
    live: false,
    pending: null,
    oddsPrev: {},      // кэфы, которые уже показали: нужно, чтобы заметить сдвиг линии
    matchesJson: "",   // снимок линии: если не изменился, экран не перерисовываем
  };

  const POLL_MS = 40000;  // как часто подтягиваем свежие кэфы, пока приложение открыто

  // ---------------------------------------------------------------- утилиты
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const money = (n) => Number(n).toLocaleString("ru-RU").replace(/\u00a0/g, " ") + " 🪙";
  const oddsText = (n) => Number(n).toFixed(2);
  const balance = () => (state.user ? state.user.balance : 0);

  function timeText(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const sameDay = d.toDateString() === now.toDateString();
    const tomorrow = new Date(now.getTime() + 86400000).toDateString() === d.toDateString();
    const day = sameDay ? "сегодня" : tomorrow ? "завтра" : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
    return `${day} ${hh}:${mm}`;
  }

  function toast(text, kind) {
    const node = $("#toast");
    node.textContent = text;
    node.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(node._t);
    node._t = setTimeout(() => { node.className = "toast"; }, 2600);
  }

  function haptic(type) {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (type === "ok") tg.HapticFeedback.notificationOccurred("success");
      else if (type === "bad") tg.HapticFeedback.notificationOccurred("error");
      else tg.HapticFeedback.impactOccurred("light");
    } catch (e) { /* не критично */ }
  }

  // ------------------------------------------------------------- localStorage
  function readCart() {
    try {
      const raw = localStorage.getItem("cs2bets_cart");
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) { return []; }
  }
  function saveCart() {
    try { localStorage.setItem("cs2bets_cart", JSON.stringify(state.cart)); } catch (e) {}
  }

  // --------------------------------------------------------------------- API
  async function api(path, options) {
    const opts = options || {};
    const headers = { "Content-Type": "application/json" };
    if (hasTelegram) headers["X-Telegram-Init-Data"] = tg.initData;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || "GET",
        headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      throw new Error(err.name === "AbortError" ? "Сервер не ответил за 15 секунд" : "Нет связи с сервером");
    } finally {
      clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = { ok: false, error: "Плохой ответ сервера" }; }
    if (!res.ok || data.ok === false) {
      const err = new Error(data.error || "Ошибка " + res.status);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function setBalance(value) {
    if (!state.user) return;
    state.user.balance = value;
    const pill = $("#balancePill");
    pill.textContent = money(value);
    pill.classList.remove("flash");
    void pill.offsetWidth;
    pill.classList.add("flash");
  }

  // ------------------------------------------------------------------ рендер
  function renderHeader() {
    // в шапке всегда бренд; имя игрока живёт в профиле (вкладка «Я»)
    const dev = state.user && state.user.dev;
    $("#userHint").textContent = dev
      ? "локальный режим · виртуальные монеты"
      : "ставки на CS2 · виртуальные монеты";
    $("#balancePill").textContent = state.user ? money(state.user.balance) : "— 🪙";
  }

  function emptyWithMascot(text) {
    // пустые состояния показываем с маскотом — приложение не выглядит «сломанным»
    const box = el("div", "empty");
    const img = el("img", "mascot-empty");
    img.src = "/static/mascot.png";
    img.alt = "";
    box.append(img, document.createTextNode(text));
    return box;
  }

  function renderFatal(message) {
    const host = $("#matches");
    host.textContent = "";
    const card = el("div", "card");
    card.append(el("div", "empty", message));
    const retry = el("button", "btn", "Обновить");
    retry.type = "button";
    retry.addEventListener("click", () => {
      host.textContent = "";
      host.append(el("div", "skeleton"));
      refreshState();
    });
    card.append(retry);
    host.append(card);
    $("#balancePill").textContent = "— 🪙";
  }

  function renderSegments() {
    document.querySelectorAll("#matchSegments .seg").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.segment === state.segment);
    });
    const count = state.liveMatches.length;
    const liveBtn = document.querySelector('#matchSegments .seg[data-segment="live"]');
    if (liveBtn) liveBtn.classList.toggle("on", count > 0);
    const badge = $("#liveCount");
    if (badge) badge.textContent = count ? String(count) : "";
  }

  function liveTag(text, kind) {
    const tag = el("span", "live-tag");
    if (kind === "live") tag.append(el("i"));
    if (kind === "wait") tag.style.color = "var(--warn)";
    tag.append(document.createTextNode(text));
    return tag;
  }

  function renderCard(match) {
    const isLive = match.status === "live";
    const isWaiting = match.status === "awaiting";
    const card = el("div", "card" + (isLive || isWaiting ? " live" : ""));

    const top = el("div", "top");
    if (isLive) top.append(liveTag("LIVE", "live"));
    else if (isWaiting) top.append(liveTag("⏳ ждём результат", "wait"));
    top.append(el("span", "tour", match.short_tournament));
    top.append(el("span", "kick", timeText(match.start_time)));
    card.append(top);

    if (match.score) {
      // табло: счёт по картам, чтобы было видно, как идёт матч
      const board = el("div", "scoreline");
      board.append(el("span", "conf", isWaiting ? "счёт в линии" : "счёт по картам"));
      const big = el("span", "big", match.score);
      if (match.status === "finished") big.classList.add("win");
      board.append(big);
      card.append(board);
    }

    const teams = el("div", "teams");
    ["a", "b"].forEach((side) => {
      const row = el("div", "team");
      const btn = el("button", "odd");
      btn.type = "button";
      const raw = side === "a" ? match.odds_a : match.odds_b;
      const name = el("span", "name", side === "a" ? match.team_a : match.team_b);
      const val = el("span", "val", oddsText(raw));
      const prev = state.oddsPrev[match.id + ":" + side];
      if (prev !== undefined && Number(prev) !== Number(raw)) {
        // линия сдвинулась: мягко подсвечиваем — вверх зелёным, вниз красным
        const grew = Number(raw) > Number(prev);
        const arrow = el("span", "arrow", grew ? "▲" : "▼");
        val.classList.add(grew ? "up" : "down");
        val.append(arrow);
        setTimeout(() => {
          val.classList.remove("up", "down");
          arrow.remove();
        }, 1400);
      }
      state.oddsPrev[match.id + ":" + side] = raw;
      if (isWaiting || match.status === "finished" || match.status === "cancelled") {
        btn.disabled = true;  // исход уже разыгран — ставить не на что
      }
      btn.append(name, val);
      btn.addEventListener("click", () => openAmountSheet(match, side));
      row.append(btn);
      teams.append(row);
    });
    card.append(teams);

    const actions = el("div", "custom");
    actions.style.margin = "9px 0 0";
    const add = el("button", "chip", "🎟 В экспресс");
    add.type = "button";
    add.addEventListener("click", () => addLeg(match));
    if (isWaiting) add.disabled = true;
    actions.append(add);
    card.append(actions);

    return card;
  }

  function renderMatches() {
    const host = $("#matches");
    host.textContent = "";
    renderSegments();

    if (!state.user) {
      const message = boot.error && /Откройте/i.test(boot.error)
        ? "Откройте приложение кнопкой в боте — тогда появятся матчи и баланс."
        : (boot.error || "Не удалось авторизоваться. Обновите экран.");
      const card = el("div", "card");
      card.append(el("div", "empty", message));
      const retry = el("button", "btn", "Обновить");
      retry.type = "button";
      retry.addEventListener("click", () => location.reload());
      card.append(retry);
      host.append(card);
      return;
    }

    const liveMode = state.segment === "live";
    const list = liveMode ? state.liveMatches : state.matches;

    if (!list.length) {
      host.append(emptyWithMascot(liveMode
        ? "Прямо сейчас никто не играет.\nМатчи подтягиваются сами — заглядывай."
        : "Сейчас нет матчей с открытыми ставками.\nЛиния обновляется сама — заходи чуть позже."));
      return;
    }

    for (const match of list) host.append(renderCard(match));
  }

  function cartOdds() {
    return state.cart.reduce((acc, leg) => acc * Number(leg.odds), 1);
  }

  function renderCartBar() {
    const bar = $("#cartbar");
    const count = state.cart.length;
    $("#tabExpress").classList.toggle("badge", count > 0);
    $("#cartDot").textContent = String(count);
    if (!count) {
      bar.classList.remove("show");
      return;
    }
    bar.classList.add("show");
    $("#cartTitle").textContent = `Экспресс: ${count} ${count === 1 ? "исход" : count < 5 ? "исхода" : "исходов"}`;
    $("#cartOdds").textContent = count < 2
      ? "добавь ещё минимум один"
      : `кэф ${oddsText(cartOdds())} · выбери сумму`;
  }

  function renderExpress() {
    const host = $("#express");
    host.textContent = "";

    if (!state.cart.length) {
      const card = el("div", "card");
      card.append(el("div", "empty", "Корзина пуста.\nОткрой матч и жми «🎟 В экспресс» — можно собрать до " + state.limits.max_legs + " исходов."));
      host.append(card);
      return;
    }

    const card = el("div", "card");
    state.cart.forEach((leg, index) => {
      const row = el("div", "rowcard");
      row.style.margin = "0 0 8px";
      const num = el("div", "rank", String(index + 1));
      const grow = el("div", "grow");
      grow.append(el("b", null, leg.team));
      grow.append(el("span", null, `${leg.tournament || ""} · кэф ${oddsText(leg.odds)}`));
      const del = el("button", "chip", "✕");
      del.type = "button";
      del.addEventListener("click", () => {
        state.cart = state.cart.filter((item) => !(item.match_id === leg.match_id));
        saveCart(); renderExpress(); renderCartBar();
        haptic("light");
      });
      row.append(num, grow, del);
      card.append(row);
    });

    const total = cartOdds();
    const summary = el("div", "summary");
    summary.innerHTML = "";
    summary.append(el("div", null, `Исходов: <b>${state.cart.length}</b> · итоговый кэф: <b>${oddsText(total)}</b>`));
    if (total > state.limits.max_odds) {
      summary.append(el("div", "hint", `Кэф выше потолка ${state.limits.max_odds} — убери лишний исход.`));
    }
    card.append(summary);

    const place = el("button", "btn", "Поставить экспресс");
    place.type = "button";
    place.addEventListener("click", () => openExpressSheet());
    card.append(place);

    const clear = el("button", "btn ghost", "Очистить");
    clear.type = "button";
    clear.style.marginTop = "8px";
    clear.addEventListener("click", () => {
      state.cart = [];
      saveCart(); renderExpress(); renderCartBar();
      toast("Корзина очищена");
    });
    card.append(clear);
    host.append(card);

    if (state.expresses.length) {
      host.append(el("h2", null, "Открытые экспрессы"));
      state.expresses.forEach((express) => {
        const row = el("div", "rowcard");
        const grow = el("div", "grow");
        grow.append(el("b", null, `Экспресс #${express.id}`));
        grow.append(el("span", null, `${money(express.stake)} × ${oddsText(express.odds)}`));
        row.append(grow);
        row.append(el("div", "money plus", money(express.potential)));
        host.append(row);
      });
    }
  }

  function renderTop(rows) {
    const host = $("#top");
    host.textContent = "";
    if (!rows || !rows.length) {
      host.append(el("div", "empty", "Пока никто не играл."));
      return;
    }
    const medals = ["🥇", "🥈", "🥉"];
    rows.forEach((row) => {
      const node = el("div", "rowcard");
      node.append(el("div", "rank", medals[row.rank - 1] || String(row.rank)));
      const grow = el("div", "grow");
      grow.append(el("b", null, row.name + (row.me ? " · вы" : "")));
      grow.append(el("span", null, row.me ? "это вы" : "игрок"));
      node.append(grow, el("div", "money", money(row.balance)));
      host.append(node);
    });
  }

  function renderHistory(items) {
    const host = $("#history");
    host.textContent = "";
    if (!items || !items.length) {
      host.append(el("div", "empty", "История пуста — сделай первую ставку."));
      return;
    }
    const card = el("div", "card");
    items.forEach((item) => {
      const row = el("div", "rowcard");
      row.style.margin = "0";
      row.style.border = "0";
      row.style.borderBottom = "1px dashed #223143";
      row.style.borderRadius = "0";
      row.style.padding = "10px 0";
      row.style.background = "transparent";
      const grow = el("div", "grow");
      grow.append(el("b", null, item.reason));
      const d = new Date(item.at * 1000);
      grow.append(el("span", null, d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })));
      row.append(grow);
      row.append(el("div", "money " + (item.delta >= 0 ? "plus" : "minus"), (item.delta >= 0 ? "+" : "−") + money(Math.abs(item.delta))));
      card.append(row);
    });
    host.append(card);
  }

  function renderMe() {
    const host = $("#me");
    host.textContent = "";
    if (!state.user) {
      host.append(el("div", "empty", "Откройте приложение кнопкой в боте."));
      return;
    }

    const card = el("div", "card");
    const kv = (label, value) => {
      const row = el("div", "kv");
      row.append(el("span", null, label));
      row.append(el("b", null, value));
      card.append(row);
    };
    kv("Игрок", state.user.name);
    kv("Баланс", money(state.user.balance));
    kv("Ставок в игре", String(state.bets.length));
    kv("Экспрессов в игре", String(state.expresses.length));
    kv("Минимальная ставка", money(state.limits.min_bet));
    host.append(card);

    const bonus = el("button", "btn", `🎁 Забрать бонус ${money(state.limits.daily_bonus)}`);
    bonus.type = "button";
    bonus.addEventListener("click", claimBonus);
    host.append(bonus);

    if (state.bets.length) {
      host.append(el("h2", null, "Ставки в игре"));
      state.bets.forEach((bet) => {
        const row = el("div", "rowcard");
        const grow = el("div", "grow");
        grow.append(el("b", null, bet.team));
        grow.append(el("span", null, `${bet.team_a} — ${bet.team_b} · кэф ${oddsText(bet.odds)}`));
        row.append(grow, el("div", "money", money(bet.amount)));
        host.append(row);
      });
    }

    host.append(el("h2", null, "История операций"));
    const historyHost = el("div");
    historyHost.id = "history";
    historyHost.append(el("div", "skeleton"));
    host.append(historyHost);
    loadHistory();

    const rules = el("div", "card");
    rules.append(el("h2", null, "Правила"));
    rules.append(el("div", "hint",
      "Кэф фиксируется в момент ставки. Ставить можно и до матча, и по ходу игры — " +
      "если линия сдвинулась после ставки, она не пересчитывается. " +
      `Выплата = ставка × кэф. Экспресс: 2–${state.limits.max_legs} исходов, кэфы перемножаются, ` +
      `потолок ${state.limits.max_odds}. Отменённый матч считается с кэфом 1.0. ` +
      "Монеты виртуальные, вывести их нельзя."));
    host.append(rules);
  }

  // --------------------------------------------------------------- действия
  function openSheet(build) {
    const wrap = $("#sheetWrap");
    const sheet = $("#sheet");
    sheet.textContent = "";
    build(sheet);
    wrap.classList.add("show");
  }
  function closeSheet() { $("#sheetWrap").classList.remove("show"); }

  function openAmountSheet(match, side) {
    haptic("light");
    const team = side === "a" ? match.team_a : match.team_b;
    const odd = side === "a" ? match.odds_a : match.odds_b;

    openSheet((sheet) => {
      sheet.append(el("h3", null, team));
      sheet.append(el("div", "sub", `${match.short_tournament} · кэф ${oddsText(odd)} · ${timeText(match.start_time)}`));

      const summary = el("div", "summary");
      const sumText = el("div");
      sumText.innerHTML = `Ставка: <b>—</b> · выигрыш: <span class="big">—</span>`;
      summary.append(sumText);
      sheet.append(summary);

      const presets = el("div", "presets");
      const amounts = [50, 100, 250, 500, 1000].filter((value) => value <= balance());
      if (!amounts.length) amounts.push(state.limits.min_bet);

      let chosen = amounts[0];

      const place = el("button", "btn", "Поставить");
      place.type = "button";
      const paint = () => {
        sumText.innerHTML = `Ставка: <b>${money(chosen)}</b> · выигрыш: <span class="big">${money(Math.round(chosen * odd))}</span>`;
      };

      const markChosen = (btn) => {
        [...presets.children].forEach((node) => { node.style.borderColor = "#2a3b4f"; node.style.background = ""; });
        if (btn) { btn.style.borderColor = "#3d8fd8"; btn.style.background = "#21384e"; }
      };

      amounts.forEach((amount) => {
        const btn = el("button", null, money(amount));
        btn.type = "button";
        btn.addEventListener("click", () => { chosen = amount; paint(); markChosen(btn); haptic("light"); });
        presets.append(btn);
      });

      const allBtn = el("button", "wide", `💥 Всё · ${money(balance())}`);
      allBtn.type = "button";
      allBtn.addEventListener("click", () => { chosen = balance(); paint(); markChosen(allBtn); haptic("light"); });
      if (balance() >= state.limits.min_bet) presets.append(allBtn);

      const custom = el("div", "custom");
      const input = el("input");
      input.type = "number";
      input.inputMode = "numeric";
      input.min = String(state.limits.min_bet);
      input.placeholder = "своя сумма";
      input.addEventListener("input", () => {
        const value = parseInt(input.value, 10);
        if (!isNaN(value) && value > 0) { chosen = value; paint(); markChosen(null); }
      });
      custom.append(input);
      const customBtn = el("button", "btn small", "OK");
      customBtn.type = "button";
      customBtn.style.flex = "0 0 74px";
      customBtn.addEventListener("click", () => { input.focus(); haptic("light"); });
      custom.append(customBtn);

      sheet.append(presets, custom);
      paint();
      markChosen(presets.children[0]);

      place.addEventListener("click", async () => {
        if (chosen < state.limits.min_bet) { toast(`Минимум ${money(state.limits.min_bet)}`, "bad"); return; }
        if (chosen > balance()) { toast("Не хватает монет", "bad"); return; }
        place.disabled = true;
        place.textContent = "Отправляю…";
        try {
          const data = await api("/api/bet", { method: "POST", body: { match_id: match.id, side, amount: chosen } });
          setBalance(data.balance);
          closeSheet();
          haptic("ok");
          toast(`Ставка принята · кэф ${oddsText(data.odds)} · выигрыш ${money(data.potential)}`, "ok");
          refreshState();
          loadBets();
        } catch (err) {
          haptic("bad");
          toast(err.message, "bad");
          place.disabled = false;
          place.textContent = "Поставить";
        }
      });
      sheet.append(place);

      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.style.marginTop = "8px";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  function openExpressSheet() {
    if (state.cart.length < 2) { toast("Нужно минимум 2 исхода", "bad"); return; }
    const total = cartOdds();
    if (total > state.limits.max_odds) { toast(`Кэф выше потолка ${state.limits.max_odds}`, "bad"); return; }

    openSheet((sheet) => {
      sheet.append(el("h3", null, `Экспресс · ${state.cart.length} исхода`));
      sheet.append(el("div", "sub", `итоговый кэф ${oddsText(total)}`));

      const summary = el("div", "summary");
      const sumText = el("div");
      summary.append(sumText);
      sheet.append(summary);

      const presets = el("div", "presets");
      const amounts = [50, 100, 250, 500, 1000].filter((value) => value <= balance());
      if (!amounts.length) amounts.push(state.limits.min_bet);
      let chosen = amounts[0];
      const paint = () => {
        sumText.innerHTML = `Ставка: <b>${money(chosen)}</b> · выигрыш: <span class="big">${money(Math.round(chosen * total))}</span>`;
      };
      const markChosen = (btn) => {
        [...presets.children].forEach((node) => { node.style.borderColor = "#2a3b4f"; node.style.background = ""; });
        if (btn) { btn.style.borderColor = "#3d8fd8"; btn.style.background = "#21384e"; }
      };
      amounts.forEach((amount) => {
        const btn = el("button", null, money(amount));
        btn.type = "button";
        btn.addEventListener("click", () => { chosen = amount; paint(); markChosen(btn); });
        presets.append(btn);
      });
      const allBtn = el("button", "wide", `💥 Всё · ${money(balance())}`);
      allBtn.type = "button";
      allBtn.addEventListener("click", () => { chosen = balance(); paint(); markChosen(allBtn); });
      if (balance() >= state.limits.min_bet) presets.append(allBtn);
      sheet.append(presets);
      paint();
      markChosen(presets.children[0]);

      const place = el("button", "btn", "Поставить экспресс");
      place.type = "button";
      place.addEventListener("click", async () => {
        if (chosen > balance()) { toast("Не хватает монет", "bad"); return; }
        place.disabled = true;
        place.textContent = "Отправляю…";
        try {
          const data = await api("/api/express", {
            method: "POST",
            body: { amount: chosen, legs: state.cart.map((leg) => ({ match_id: leg.match_id, side: leg.side })) },
          });
          setBalance(data.balance);
          state.cart = [];
          saveCart();
          closeSheet();
          haptic("ok");
          toast(`Экспресс #${data.express_id} принят · кэф ${oddsText(data.odds)}`, "ok");
          renderCartBar();
          refreshState();
          loadBets();
          switchTab("express");
        } catch (err) {
          haptic("bad");
          toast(err.message, "bad");
          place.disabled = false;
          place.textContent = "Поставить экспресс";
        }
      });
      sheet.append(place);

      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.style.marginTop = "8px";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  function addLeg(match) {
    if (state.cart.some((leg) => leg.match_id === match.id)) {
      toast("Этот матч уже в корзине", "bad");
      return;
    }
    if (state.cart.length >= state.limits.max_legs) {
      toast(`Максимум ${state.limits.max_legs} исходов`, "bad");
      return;
    }
    openSheet((sheet) => {
      sheet.append(el("h3", null, "Какой исход берём?"));
      sheet.append(el("div", "sub", match.short_tournament));
      const pick = (side) => {
        const team = side === "a" ? match.team_a : match.team_b;
        const odd = side === "a" ? match.odds_a : match.odds_b;
        state.cart.push({
          match_id: match.id,
          side,
          team,
          odds: odd,
          tournament: match.short_tournament,
        });
        saveCart();
        closeSheet();
        renderExpress();
        renderCartBar();
        haptic("ok");
        const total = cartOdds();
        toast(`Добавлено: ${team} · кэф ${oddsText(total)}` + (state.cart.length < 2 ? " · нужен ещё исход" : ""), "ok");
      };
      [["a", match.team_a, match.odds_a], ["b", match.team_b, match.odds_b]].forEach(([side, team, odd]) => {
        const btn = el("button", "btn ghost");
        btn.type = "button";
        btn.style.marginBottom = "8px";
        btn.style.justifyContent = "space-between";
        btn.append(el("span", null, team), el("b", null, oddsText(odd)));
        btn.addEventListener("click", () => pick(side));
        sheet.append(btn);
      });
      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  async function claimBonus() {
    try {
      const data = await api("/api/bonus", { method: "POST" });
      setBalance(data.balance);
      if (data.received) {
        haptic("ok");
        toast(`Бонус ${money(data.amount)} зачислен`, "ok");
      } else {
        const hours = Math.floor(data.wait_seconds / 3600);
        const minutes = Math.floor((data.wait_seconds % 3600) / 60);
        toast(`Бонус уже получен. Следующий через ${hours} ч ${minutes} мин`, "bad");
      }
      refreshState();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  // ------------------------------------------------------------- навигация
  function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".screen").forEach((node) => node.classList.remove("active"));
    const screen = document.getElementById("screen-" + tab);
    if (screen) screen.classList.add("active");
    document.querySelectorAll("nav.tabs button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    if (tab === "bets") loadBets();
    if (tab === "top") loadTop();
    if (tab === "me") renderMe();
    if (tab === "express") renderExpress();
    haptic("light");
  }

  // ------------------------------------------------------- история ставок
  function betStatusTag(item) {
    if (item.kind === "express") {
      if (item.status === "open") return ["live", "в игре"];
    }
    if (item.status === "open") return ["open", "ждёт старта"];
    if (item.status === "live") return ["live", "матч идёт"];
    if (item.result === "won") return ["won", "зашла"];
    if (item.result === "lost") return ["lost", "не зашла"];
    if (item.result === "void") return ["void", "возврат"];
    return ["open", "—"];
  }

  function betTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function renderBetCard(item) {
    const [cls, label] = betStatusTag(item);
    const card = el("div", "betcard " + cls);

    const head = el("div", "bet-head");
    const grow = el("div", "grow");
    grow.append(el("b", null, item.title));
    const sub = item.kind === "express"
      ? `${item.stake ? money(item.stake) : ""} · кэф ${oddsText(item.odds)}`
      : `${item.subtitle} · кэф ${oddsText(item.odds)}`;
    grow.append(el("span", null, sub));
    head.append(grow, el("span", "tag " + cls, label));
    card.append(head);

    if (item.legs && item.legs.length) {
      const legsBox = el("div", "bet-legs");
      item.legs.forEach((leg) => {
        const mark = leg.result === "won" ? "✅" : leg.result === "lost" ? "❌" : leg.result === "void" ? "⚪️" : "•";
        legsBox.append(el("div", null, `${mark} ${leg.team} · кэф ${oddsText(leg.odds)}`));
      });
      card.append(legsBox);
    }

    const meta = el("div", "bet-meta");
    const left = el("span", null, "");
    left.append(document.createTextNode("Ставка "));
    left.append(el("b", null, money(item.stake)));
    meta.append(left);

    const right = el("span", null, "");
    if (item.status === "open" || item.status === "live") {
      right.append(document.createTextNode("Возможная выплата "));
      right.append(el("b", null, money(item.potential)));
    } else {
      const paid = Number(item.payout || 0);
      const net = paid - Number(item.stake || 0);
      if (item.result === "lost") {
        right.append(document.createTextNode("Проигрыш"));
      } else {
        right.append(document.createTextNode(item.result === "void" ? "Возврат " : "Выплата "));
        right.append(el("b", null, money(paid)));
      }
      if (item.result !== "void") {
        right.append(el("span", "money " + (net >= 0 ? "plus" : "minus"),
          ` ${net >= 0 ? "+" : "−"}${money(Math.abs(net))}`));
      }
    }
    meta.append(right);
    card.append(meta);
    return card;
  }

  function renderBets(data) {
    const host = $("#bets");
    host.textContent = "";
    const open = (data && data.open) || [];
    const settled = (data && data.settled) || [];

    if (!open.length && !settled.length) {
      host.append(emptyWithMascot("Ставок пока нет. Матчи → кэф → сумма."));
      return;
    }

    const openTitle = el("div", "section-title");
    openTitle.append(el("h2", null, "В игре"));
    openTitle.append(el("span", "tag open", String(open.length)));
    host.append(openTitle);
    if (!open.length) {
      host.append(emptyWithMascot("Нет активных ставок."));
    } else {
      open.forEach((item) => host.append(renderBetCard(item)));
    }

    const settledTitle = el("div", "section-title");
    settledTitle.append(el("h2", null, "Рассчитанные"));
    if (data && data.counts) {
      settledTitle.append(el("span", "tag won", `зашло ${data.counts.won} · не зашло ${data.counts.lost}`));
    }
    host.append(settledTitle);
    if (!settled.length) {
      host.append(el("div", "empty", "Пока ничего не рассчитано."));
    } else {
      settled.forEach((item) => host.append(renderBetCard(item)));
    }
  }

  async function loadBets() {
    const host = $("#bets");
    if (!host) return;
    try {
      const data = await api("/api/bets");
      state.betsHistory = data;
      renderBets(data);
      const dot = $("#betsDot");
      if (dot) {
        const openCount = (data.open || []).length;
        dot.textContent = openCount ? String(openCount) : "0";
        dot.parentElement.classList.toggle("badge", openCount > 0);
      }
    } catch (err) {
      host.textContent = "";
      host.append(el("div", "empty", err.message));
    }
  }

  async function loadTop() {
    try {
      const data = await api("/api/top");
      renderTop(data.top);
    } catch (err) {
      $("#top").textContent = "";
      $("#top").append(el("div", "empty", err.message));
    }
  }

  async function loadHistory() {
    const host = $("#history");
    if (!host) return;  // блок истории живёт внутри профиля
    try {
      const data = await api("/api/history");
      renderHistory(data.items);
    } catch (err) {
      host.textContent = "";
      host.append(el("div", "empty", err.message));
    }
  }

  async function refreshState(options) {
    const silent = Boolean(options && options.silent);  // тихий поллинг: без тостов об ошибках
    try {
      const data = await api("/api/state");
      const liveMatches = data.live || [];
      const matchesJson = JSON.stringify([data.matches, liveMatches]);
      const matchesChanged = matchesJson !== state.matchesJson;
      const balanceBefore = state.user ? state.user.balance : null;

      state.user = data.user;
      state.matches = data.matches;
      state.liveMatches = liveMatches;
      state.matchesJson = matchesJson;
      state.bets = data.bets;
      state.expresses = data.expresses;
      state.limits = data.limits;
      state.live = true;

      renderHeader();
      if (balanceBefore !== null && data.user && Number(data.user.balance) !== Number(balanceBefore)) {
        setBalance(data.user.balance);   // выплата прилетела, пока экран открыт
      }
      if (matchesChanged) renderMatches();  // без изменений не перерисовываем: не мешаем читать
      renderCartBar();
      if (state.tab === "express") renderExpress();
      if (state.tab === "me") renderMe();
    } catch (err) {
      if (err.status === 401) {
        state.user = boot.user || null;
        renderHeader();
        renderMatches();
        return;
      }
      if (silent) return;  // фоновая попытка не должна ругаться на экране
      // если сервер недоступен, но данные пришли вместе со страницей — не стираем экран
      if (state.user && state.matches.length) {
        toast("Превью-режим: показан снимок данных, ставки требуют сервера", "bad");
        return;
      }
      renderFatal(err.message || "Сервер недоступен");
    }
  }

  // ------------------------------------------------------------------- init
  function init() {
    renderHeader();
    renderMatches();
    renderExpress();
    renderCartBar();
    renderMe();
    switchTab("matches");

    document.querySelectorAll("nav.tabs button").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    document.querySelectorAll("#matchSegments .seg").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (state.segment === btn.dataset.segment) return;
        state.segment = btn.dataset.segment;
        haptic("light");
        renderMatches();
        if (state.segment === "live") refreshState({ silent: true });  // свежий счёт сразу
      });
    });
    $("#cartOpen").addEventListener("click", () => switchTab("express"));
    $("#sheetWrap").addEventListener("click", (event) => {
      if (event.target === $("#sheetWrap")) closeSheet();
    });

    if (tg) {
      try {
        tg.ready();
        tg.expand();
        if (tg.setHeaderColor) tg.setHeaderColor("#0f1720");
        if (tg.setBackgroundColor) tg.setBackgroundColor("#0f1720");
        if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
        const theme = tg.themeParams || {};
        if (theme.button_color) document.documentElement.style.setProperty("--accent", theme.button_color);
      } catch (e) { /* старые клиенты */ }
    }

    refreshState();
    loadBets();   // заодно ставит бейдж с числом ставок в игре

    // автообновление: кэфы и статусы матчей подтягиваются без перезахода
    setInterval(() => {
      if (document.hidden) return;   // в фоне не дёргаем сервер
      refreshState({ silent: true });
    }, POLL_MS);

    // вернулись в приложение — сразу свежая линия
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshState({ silent: true });
    });
  }

  document.addEventListener("DOMContentLoaded", init);
  if (document.readyState !== "loading") init();
})();
