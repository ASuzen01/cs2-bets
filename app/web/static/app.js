(() => {
  "use strict";

  const boot = window.__BOOT__ || { user: null, matches: [], error: null, dev: false };
  // Telegram SDK загружается async: не фиксируем shim при старте — приложение
  // могло бы навсегда остаться без initData, если SDK появился чуть позже.
  const tg = new Proxy({}, {
    get(_target, key) {
      const app = (window.Telegram && window.Telegram.WebApp) || window.__TG_SHIM__ || null;
      const value = app && app[key];
      return typeof value === "function" ? value.bind(app) : value;
    },
  });
  const hasTelegram = () => Boolean(tg.initData);

  const state = {
    user: boot.user || null,
    authPending: !boot.user && !boot.dev,
    matches: boot.matches || [],
    football: boot.football || [],
    hockey: boot.hockey || [],
    sport: "cs2",
    // подменю футбола и хоккея: "clubs" — клубы, "national" — сборные
    category: "clubs",
    fbOpen: {},          // какие футбольные карточки раскрыты до полной росписи
    tournaments: {},     // выбранный турнир по каждому виду спорта
    liveMatches: boot.live || [],   // матчи в эфире (state.live — флаг «данные загружены»)
    segment: "line",                // «Линия» / «Live» внутри вкладки матчей
    bets: [],
    expresses: [],
    limits: { min_bet: 10, max_legs: 0, max_odds: 0, daily_bonus: 250, ladder_max_bet: 50 },
    cart: readCart(),
    betsView: "all",
    tickets: [],       // билеты в экспресс с колеса
    topMode: "all",
    chain: null,
    achievements: null,
    newAchievements: [],
    tab: "matches",
    live: false,
    pending: null,
    bonus: null,       // ежедневный бонус, колесо и пасхалка (приходит из /api/state)
    wheelResult: "",   // текст последнего результата: переживает перерисовку
    wheelAngle: 0,     // текущий угол колеса
    wheelBusy: false,
    quests: boot.quests || [],   // задания дня (приходят из /api/state)
    stats: null,       // итоги игрока для «Я» (/api/stats)
    favorites: readFavorites(),  // любимые команды: живут в localStorage
    onlyFavorites: false,
    teaseTaps: 0,      // сколько раз подряд жали на бонус
    oddsPrev: {},      // кэфы, которые уже показали: нужно, чтобы заметить сдвиг линии
    matchesJson: "",   // снимок линии: если не изменился, экран не перерисовываем
    // ---------------------------------------------------------------- игры
    casino: null,             // роспись игр, сводка и история (из /api/state)
    blackjack: null,          // последняя серверная раздача блэкджека
    blackjackStake: 50,
    blackjackBusy: false,
    blackjackTyping: false,
    ladder: null,             // последняя партия в «Лесенку»
    ladderStake: 10,
    ladderBusy: false,
    crash: null,               // активный/последний полёт «Самолётика» с сервера
    crashStake: 50,
    crashBusy: false,
    crashPolling: false,
    crashTicker: null,
    game: null,                // null — меню игр, иначе открыта выбранная игра
    casinoPick: { roulette: "red", dice: "over" },
    casinoStake: { roulette: 50, dice: 50 },
    casinoBusy: false,
    casinoTyping: false,     // игрок вводит сумму: перерисовку не мешаем
    casinoResults: { roulette: null, dice: null },  // результат своей игры у каждой игры
    casinoToken: null,        // метка тапа текущего раунда (защита от двойного списания)
    recentNumbers: [],        // дорожка выпавших чисел
    rouletteAngle: 0,
    ballAngle: 0,
    diceAngles: [{ x: -18, y: 22 }, { x: -14, y: -26 }],
    diceShown: null,
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

  // В системных шрифтах Windows/Linux эмодзи 🪙 может отсутствовать.
  // Заменяем его локальной SVG-иконкой, не зависящей от emoji-шрифтов.
  const COIN_EMOJI = "🪙";
  const SVG_NS = "http://www.w3.org/2000/svg";
  function coinIcon() {
    const label = document.createElement("span");
    label.className = "currency-coin";
    label.setAttribute("role", "img");
    label.setAttribute("aria-label", "виртуальные монеты");
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-coin");
    svg.append(use);
    label.append(svg);
    return label;
  }
  function replaceCoinText(node) {
    if (!node || !node.nodeValue || !node.nodeValue.includes(COIN_EMOJI)) return;
    const text = node.nodeValue;
    const parent = node.parentElement;
    if (parent && parent.namespaceURI === SVG_NS) {
      // SVG <text> не принимает HTML-иконки: используем стабильный текстовый fallback.
      node.nodeValue = text.replaceAll(COIN_EMOJI, "◉");
      return;
    }
    const fragment = document.createDocumentFragment();
    const parts = text.split(COIN_EMOJI);
    parts.forEach((part, index) => {
      if (index < parts.length - 1 && /\s$/.test(part)) {
        // Не оставляем значок на следующей строке отдельно от суммы.
        part = part.slice(0, -1) + "\u00a0";
      }
      if (part) fragment.append(document.createTextNode(part));
      if (index < parts.length - 1) {
        const next = parts[index + 1];
        const punctuation = next.match(/^[.,;:!?…]/);
        const unit = document.createElement("span");
        unit.className = "currency-unit";
        unit.append(coinIcon());
        if (punctuation) {
          unit.append(document.createTextNode(punctuation[0]));
          parts[index + 1] = next.slice(punctuation[0].length);
        }
        fragment.append(unit);
      }
    });
    node.replaceWith(fragment);
  }
  function replaceCoinEmoji(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      replaceCoinText(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE || /^(SCRIPT|STYLE|TEXTAREA)$/.test(root.tagName)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    textNodes.forEach(replaceCoinText);
  }
  replaceCoinEmoji(document.body);
  new MutationObserver((records) => {
    records.forEach((record) => {
      if (record.type === "characterData") replaceCoinText(record.target);
      record.addedNodes.forEach(replaceCoinEmoji);
    });
  }).observe(document.body, { childList: true, subtree: true, characterData: true });

  const money = (n) => Number(n).toLocaleString("ru-RU").replace(/\u00a0/g, " ") + " 🪙";
  // для строк, где перенос внутри суммы выглядит как ошибка
  const moneySolid = (n) => money(n).replace(/ /g, "\u00a0");
  // прибыль недели бывает отрицательной — показываем со знаком
  const moneySigned = (n) => (Number(n) > 0 ? "+" : "") + money(n);
  // Python round() в API — half-to-even; повторяем это в браузере, чтобы
  // возможная выплата в интерфейсе совпадала с фактической выплатой сервера.
  function roundPayout(amount, odds) {
    const value = Number(amount) * Number(odds);
    if (!Number.isFinite(value) || value <= 0) return 0;
    const lower = Math.floor(value);
    const fraction = value - lower;
    if (Math.abs(fraction - 0.5) < 1e-8) return lower % 2 === 0 ? lower : lower + 1;
    return Math.round(value);
  }
  // кэф может быть снят (идёт матч, Fonbet убрал рынок) — тогда прочерк, а не 0.00
  const oddsText = (n) => (n === null || n === undefined ? "—" : Number(n).toFixed(2));
  const balance = () => (state.user ? state.user.balance : 0);
  const minBet = () => Math.max(1, Number((state.limits && state.limits.min_bet) || 10));
  const jackpot = () => Number((state.bonus && state.bonus.wheel && state.bonus.wheel.jackpot) || 0);
  // кнопка не должна предлагать действие, которое сервер всё равно отклонит
  // ставить можно и с пустым балансом, если есть живой фрибет
  const freebetOf = () => (state.tickets || []).find((item) => item.kind === "single") || null;
  const canBet = () => Boolean(state.user) && (balance() >= minBet() || Boolean(freebetOf()));

  // метка тапа: одинаковый токен в повторном запросе = одна и та же ставка
  function newToken() {
    try {
      if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    } catch (e) { /* старый вебвью */ }
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

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
    node._t = setTimeout(() => { node.className = "toast"; }, 2400);
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

  function readFavorites() {
    try {
      const raw = localStorage.getItem("cs2bets_fav");
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((name) => typeof name === "string") : [];
    } catch (e) { return []; }
  }
  function saveFavorites() {
    try { localStorage.setItem("cs2bets_fav", JSON.stringify(state.favorites)); } catch (e) {}
  }
  const isFavorite = (name) => state.favorites.indexOf(name) !== -1;
  function toggleFavorite(name) {
    if (!name) return;
    state.favorites = isFavorite(name)
      ? state.favorites.filter((item) => item !== name)
      : state.favorites.concat([name]).slice(0, 40);
    saveFavorites();
    haptic("light");
    toast(isFavorite(name) ? `★ ${name} в избранном` : `${name} убран из избранного`);
  }

  // --------------------------------------------------------------------- API
  let authWaitAttempted = false;
  function pause(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  let telegramInitializedApp = null;
  function initializeTelegram() {
    const app = window.Telegram && window.Telegram.WebApp;
    if (!app || app === telegramInitializedApp) return;
    telegramInitializedApp = app;
    try {
      app.ready();
      app.expand();
      if (app.setHeaderColor) app.setHeaderColor("#0f1720");
      if (app.setBackgroundColor) app.setBackgroundColor("#0f1720");
      if (app.disableVerticalSwipes) app.disableVerticalSwipes();
      const theme = app.themeParams || {};
      if (theme.button_color) document.documentElement.style.setProperty("--accent", theme.button_color);
    } catch (e) { /* SDK ещё инициализируется; retry вызовет setup ещё раз при замене объекта */ }
  }
  async function waitForTelegramData(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (true) {
      initializeTelegram();
      if (hasTelegram()) return true;
      if (Date.now() >= deadline) return false;
      await pause(100);
    }
  }

  async function api(path, options) {
    const opts = options || {};
    // На некоторых клиентах SDK приходит после app.js. Даём ему немного времени,
    // прежде чем посылать первый запрос без initData и показывать ошибку входа.
    if (path === "/api/state" && !hasTelegram() && !boot.dev && !boot.user && !authWaitAttempted) {
      authWaitAttempted = true;
      await waitForTelegramData(1800);
    }
    const headers = { "Content-Type": "application/json" };
    if (hasTelegram()) headers["X-Telegram-Init-Data"] = tg.initData;

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
    // в шапке только бренд: имя и режим живут в профиле (вкладка «Я»)
    $("#balancePill").textContent = state.user ? money(state.user.balance) : "— 🪙";
    renderBonusDots();
  }

  function bonusReady() {
    const bonus = state.bonus;
    const questsReady = (state.quests || []).filter((quest) => quest.ready).length;
    if (!bonus) return questsReady;
    return (bonus.daily.available ? 1 : 0) + (bonus.wheel.available ? 1 : 0) + questsReady;
  }

  function renderBonusDots() {
    // вкладка бонусов живёт только в шапке: точка на конверте — есть что забрать
    const dot = $("#bonusDot");
    if (dot) dot.hidden = bonusReady() === 0;
    const bell = $("#bonusBell");
    if (bell) bell.classList.toggle("active", state.tab === "bonus");
  }

  function emptyWithMascot(text) {
    // пустые состояния получаются с эмблемой клуба, а не выглядят как ошибка
    const box = el("div", "empty");
    const img = el("img", "mascot-empty");
    img.src = "/static/crest-clover.svg";
    img.alt = "";
    box.append(img, document.createTextNode(text));
    return box;
  }

  // пустое состояние с полезным действием: не тупик, а следующий шаг
  function emptyAction(text, label, onTap) {
    const box = emptyWithMascot(text);
    const btn = el("button", "btn ghost", label);
    btn.type = "button";
    btn.style.marginTop = "12px";
    btn.addEventListener("click", onTap);
    box.append(btn);
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

  // Слушатель на статическую кнопку — строго один раз, сколько бы раз её ни перерисовывали.
  function once(node, handler) {
    if (!node || node.dataset.bound === "1") return;
    node.dataset.bound = "1";
    node.addEventListener("click", handler);
  }

  function renderSegments() {
    const tourBtn = $("#tourFilter");
    if (tourBtn) {
      once(tourBtn, () => {
        const source = state.segment === "live"
          ? state.liveMatches
          : marketSource();
        openTournamentSheet(source
          .filter((m) => (m.sport || "cs2") === state.sport)
          .filter(inCategory));
      });
    }
    const tourReset = $("#tourReset");
    if (tourReset) {
      once(tourReset, () => { setTournament(null); renderMatches(); });
    }
    document.querySelectorAll("#sportSegments .seg").forEach((btn) => {
      once(btn, () => {
        if (state.sport === btn.dataset.sport) return;
        state.sport = btn.dataset.sport;
        haptic("light");
        renderMatches();
        refreshState({ silent: true });
      });
    });
    // «Клубы / Сборные»: фильтр турнира сбрасываем — он был из другой категории
    document.querySelectorAll("#categorySegments .seg").forEach((btn) => {
      once(btn, () => {
        if (state.category === btn.dataset.category) return;
        state.category = btn.dataset.category;
        setTournament(null);
        haptic("light");
        renderMatches();
      });
    });
    document.querySelectorAll("#matchSegments .seg").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.segment === state.segment);
    });
    // счётчик Live — только по текущему виду спорта, иначе обещает больше, чем есть
    const count = state.liveMatches.filter((m) => (m.sport || "cs2") === state.sport).length;
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

  function oddButton(match, side, value, flashKey, onPick, compact) {
    const btn = el("button", "odd" + (compact ? " small" : ""));
    btn.type = "button";
    const name = el("span", "name", side === "a" ? match.team_a : match.team_b);
    const val = el("span", "val", oddsText(value));

    const prev = state.oddsPrev[flashKey];
    if (prev !== undefined && Number(prev) !== Number(value)) {
      // линия сдвинулась: мягко подсвечиваем — вверх зелёным, вниз красным
      const grew = Number(value) > Number(prev);
      const arrow = el("span", "arrow", grew ? "▲" : "▼");
      val.classList.add(grew ? "up" : "down");
      val.append(arrow);
      setTimeout(() => {
        val.classList.remove("up", "down");
        arrow.remove();
      }, 1400);
    }
    state.oddsPrev[flashKey] = value;

    // матч уже начался, а фид не успел перевести его в live: сервер такую ставку отклонит
    const started = match.status === "open" && Number(match.start_time) * 1000 <= Date.now();
    if (value === null || value === undefined) {
      // кэф снят (идёт карта или матч уже начался) — ставить не на что
      btn.disabled = true;
      btn.classList.add("off");
    } else if (started) {
      btn.disabled = true;
      btn.classList.add("off");
      btn.title = "Матч уже начался";
    }
    btn.append(name, val);
    btn.addEventListener("click", onPick);
    return btn;
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
    top.append(favoriteStar(match));
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

    const closed = isWaiting || match.status === "finished" || match.status === "cancelled";
    const maps = Array.isArray(match.maps) ? match.maps : [];

    if (maps.length) card.append(el("div", "market-title", "Победа в матче"));

    const teams = el("div", "teams");
    ["a", "b"].forEach((side) => {
      const row = el("div", "team");
      const btn = oddButton(
        match, side, side === "a" ? match.odds_a : match.odds_b,
        match.id + ":" + side,
        () => openAmountSheet(match, side),
      );
      if (closed) btn.disabled = true;  // исход уже разыгран — ставить не на что
      row.append(btn);
      teams.append(row);
    });
    card.append(teams);

    if (maps.length) {
      // отдельные рынки: кто заберёт конкретную карту
      const box = el("div", "maps");
      maps.forEach((card_market) => {
        const row = el("div", "maprow");
        // На узком экране «Карта 1» съедает место у названий команд — там показываем «К1»,
        // выбор варианта делает CSS (медиазапрос), рендер одинаковый.
        const label = el("span", "maplabel");
        label.append(
          el("span", "lbl-full", `Карта ${card_market.number}`),
          el("span", "lbl-short", `К${card_market.number}`),
        );
        if (card_market.score) label.append(el("span", "mapscore", card_market.score));
        // карта живёт своей жизнью: серия может ждать результат, а карта — идти
        const mapOpen = card_market.status === "open" || card_market.status === "live";
        if (!mapOpen) {
          const hint = card_market.status === "awaiting" ? "ждём"
            : card_market.status === "cancelled" ? "возврат" : "готово";
          label.append(el("span", "maphint", hint));
        }
        row.append(label);
        const closedMap = closed || !mapOpen;
        ["a", "b"].forEach((side) => {
          const btn = oddButton(
            match, side,
            side === "a" ? card_market.odds_a : card_market.odds_b,
            "m" + card_market.id + ":" + side,
            () => openAmountSheet(match, side, card_market),
            true,
          );
          if (closedMap) btn.disabled = true;
          row.append(btn);
        });
        box.append(row);
      });
      card.append(box);
    }

    card.append(legAddRow(match, () => [
      { side: "a", name: match.team_a, odds: match.odds_a },
      { side: "b", name: match.team_b, odds: match.odds_b },
    ], isWaiting));

    return card;
  }

  // «В экспресс» под карточкой матча: один вид кнопки на CS2, футбол и хоккей
  function legAddRow(match, variants, waiting) {
    const actions = el("div", "custom");
    actions.style.margin = "9px 0 0";
    const inCart = state.cart.find((leg) => leg.match_id === match.id);
    const add = el("button", "legadd" + (inCart ? " added" : ""));
    add.type = "button";
    add.dataset.match = String(match.id);
    add.append(icon(inCart ? "check" : "ticket"));
    add.append(el("span", null, inCart ? "В экспрессе" : "Добавить в экспресс"));
    if (inCart) {
      // повторный тап убирает ногу — не нужно лезть в корзину, чтобы передумать
      add.append(el("span", "count", (inCart.team || labelForSide(match, inCart.side)) + " · убрать"));
      add.addEventListener("click", () => removeLeg(match.id));
    } else {
      add.append(el("span", "count", "＋"));
      add.addEventListener("click", () => addLeg(match, variants));
    }
    // матч начался (или ждёт результата) — в экспресс такой исход уже не собрать
    const startedOpen = match.status === "open" && Number(match.start_time) * 1000 <= Date.now();
    if (waiting || startedOpen) {
      add.disabled = true;
      if (startedOpen) add.append(el("span", "count", "матч начался"));
    }
    actions.append(add);
    return actions;
  }

  function teamName(match, side) {
    if (side === "a") return match.team_a;
    if (side === "b") return match.team_b;
    if (side === "x") return match && match.sport === "hockey" ? "Ничья (осн. время)" : "Ничья";
    return side;
  }

  function sideOdds(match, side) {
    if (side === "a") return match.odds_a;
    if (side === "b") return match.odds_b;
    if (side === "x") return match.odds_x;
    return null;
  }

  // рынок по коду исхода: `f:total:over:2.5` / `h:final:1` → объект росписи
  function marketOfSide(match, side) {
    const code = String(side || "");
    if (code === "a" || code === "x" || code === "b") return null;
    const prefix = match && match.sport === "hockey" ? "h:" : "f:";
    if (!code.startsWith(prefix)) return null;
    const [, key, param] = code.split(":", 3);
    if (!key || param === undefined) return null;
    const markets = Array.isArray(match.markets) ? match.markets : [];
    return markets.find((market) => market.key === key && market.param === param) || null;
  }

  // кэф по коду исхода: исход матча берём из линии, рынок — из росписи
  function oddsForSide(match, side) {
    const market = marketOfSide(match, side);
    if (market) return market.odds;
    return sideOdds(match, side);
  }

  // подпись ноги экспресса: команда, ничья или название рынка
  function labelForSide(match, side) {
    if (side === "a") return match.team_a + (match.sport === "hockey" ? " (осн. время)" : "");
    if (side === "b") return match.team_b + (match.sport === "hockey" ? " (осн. время)" : "");
    if (side === "x") return match && match.sport === "hockey" ? "Ничья (осн. время)" : "Ничья";
    const market = marketOfSide(match, side);
    return market ? market.label : String(side);
  }

  function tournamentKey() {
    return "ludka:tour:" + state.sport;
  }

  // матчи текущей вкладки: у CS2 — основная линия, у футбола и хоккея — своя
  function marketSource() {
    if (state.sport === "football") return state.football;
    if (state.sport === "hockey") return state.hockey;
    return state.matches;
  }

  // у футбола и хоккея есть подменю «Клубы / Сборные»
  function hasCategories() {
    return state.sport === "football" || state.sport === "hockey";
  }

  function inCategory(match) {
    if (!hasCategories()) return true;
    return (match.category || "clubs") === state.category;
  }

  function categoryWord() {
    return state.category === "national" ? "сборных" : "клубов";
  }

  function renderCategorySegments() {
    const host = $("#categorySegments");
    if (!host) return;
    host.hidden = !hasCategories();
    host.querySelectorAll(".seg").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.category === state.category);
    });
  }

  function selectedTournament() {
    if (state.tournaments[state.sport] !== undefined) return state.tournaments[state.sport];
    try { return localStorage.getItem(tournamentKey()); } catch (err) { return null; }
  }

  function setTournament(name) {
    state.tournaments[state.sport] = name;
    try {
      if (name) localStorage.setItem(tournamentKey(), name);
      else localStorage.removeItem(tournamentKey());
    } catch (err) { /* приватный режим — просто не запоминаем */ }
  }

  function tournamentsOf(list) {
    const counts = new Map();
    list.forEach((match) => {
      const name = match.short_tournament || match.tournament || "Без турнира";
      counts.set(name, (counts.get(name) || 0) + 1);
    });
    return [...counts.entries()]
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }

  function renderTournamentFilter(all) {
    const button = $("#tourFilter");
    if (!button) return;
    const chosen = selectedTournament();
    const list = tournamentsOf(all);
    // выбранный турнир мог уйти из линии — тогда тихо показываем «все»
    if (chosen && !list.some((item) => item.name === chosen)) setTournament(null);
    const active = selectedTournament();
    button.textContent = "";
    button.append(document.createTextNode(active || "Все турниры"));
    const caret = el("span", "caret", "▼");
    button.append(caret);
    button.classList.toggle("active", Boolean(active));
    const reset = $("#tourReset");
    if (reset) reset.hidden = !active;
  }

  function openTournamentSheet(all) {
    haptic("light");
    const list = tournamentsOf(all);
    const active = selectedTournament();
    openSheet((sheet) => {
      sheet.append(el("h3", null, "Турниры"));
      sheet.append(el("div", "sub", `${list.length} турниров · ${all.length} матчей`));
      const box = el("div", "tourlist");
      const pick = (name) => {
        setTournament(name);
        closeSheet();
        renderMatches();
      };
      const allBtn = el("button", !active ? "active" : "");
      allBtn.type = "button";
      allBtn.append(el("span", "nm", "Все турниры"));
      allBtn.append(el("span", "cnt", String(all.length)));
      allBtn.addEventListener("click", () => pick(null));
      box.append(allBtn);

      list.forEach((item) => {
        const btn = el("button", item.name === active ? "active" : "");
        btn.type = "button";
        btn.append(el("span", "nm", item.name));
        btn.append(el("span", "cnt", String(item.count)));
        btn.addEventListener("click", () => pick(item.name));
        box.append(btn);
      });
      sheet.append(box);
    });
  }

  function renderSportSegments() {
    const tourBtn = $("#tourFilter");
    if (tourBtn) {
      tourBtn.addEventListener("click", () => {
        const source = state.segment === "live"
          ? state.liveMatches
          : marketSource();
        openTournamentSheet(source
          .filter((m) => (m.sport || "cs2") === state.sport)
          .filter(inCategory));
      });
    }
    const tourReset = $("#tourReset");
    if (tourReset) {
      tourReset.addEventListener("click", () => { setTournament(null); renderMatches(); });
    }
    document.querySelectorAll("#sportSegments .seg").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.sport === state.sport);
    });
  }

  function renderFootballCard(match) {
    const isLive = match.status === "live";
    // в live ставим только на исход: кэфы 1X2 идут из линии живьём,
    // а производные рынки считаются от предматчевых кэфов и закрыты
    const closed = match.status !== "open" && match.status !== "live";
    const card = el("div", "card" + (isLive ? " live" : ""));

    const top = el("div", "top");
    if (isLive) top.append(liveTag("LIVE", "live"));
    top.append(el("span", "tour", match.short_tournament));
    top.append(el("span", "kick", timeText(match.start_time)));
    card.append(top);

    const board = el("div", "fb-board");
    const teams = el("div", "fb-teams");
    teams.append(document.createTextNode(match.team_a));
    teams.append(el("span", null, "—"));
    teams.append(document.createTextNode(match.team_b));
    board.append(teams);
    if (match.score) board.append(el("div", "fb-score" + (isLive ? " live" : ""), match.score));
    card.append(board);

    card.append(el("div", "market-title", "Исход"));
    const three = el("div", "fb-odds");
    [["a", "П1"], ["x", "X"], ["b", "П2"]].forEach(([side, label]) => {
      const value = sideOdds(match, side);
      const btn = el("button", "odd");
      btn.type = "button";
      btn.append(el("span", "name", label));
      btn.append(el("span", "val", value ? oddsText(value) : "—"));
      btn.disabled = closed || !value;
      btn.addEventListener("click", () => openAmountSheet(match, side));
      three.append(btn);
    });
    card.append(three);

    if (isLive) {
      card.append(el("div", "hint live-hint", "Ставки на исход открыты · роспись — до старта"));
    }

    // в экспресс по футболу берём исход: П1 / X / П2 — ровно те кэфы, что в линии
    card.append(legAddRow(match, () => [
      { side: "a", name: match.team_a, odds: match.odds_a },
      { side: "x", name: "Ничья", odds: match.odds_x },
      { side: "b", name: match.team_b, odds: match.odds_b },
    ], closed));

    const markets = (Array.isArray(match.markets) ? match.markets : [])
      .filter((market) => market.key !== "result");
    if (markets.length && match.status === "open") {
      const open = Boolean(state.fbOpen[match.id]);
      const toggle = el("button", "mkt-toggle" + (open ? " open" : ""));
      toggle.type = "button";
      toggle.append(el("span", null, open ? "Свернуть роспись" : "Вся роспись"));
      toggle.append(el("span", "cnt", markets.length + " " + marketsWord(markets.length)));
      toggle.addEventListener("click", () => {
        state.fbOpen[match.id] = !open;
        haptic("light");
        renderMatches();
      });
      card.append(toggle);
      if (open) card.append(renderMarketGroups(match, markets));
    }

    return card;
  }

  function renderHockeyCard(match) {
    // Хоккей: «Основное время» (П1/X/П2 — кэфы книги) и «Победа в матче» (с ОТ).
    // В live остаются только кэфы линии на основное время — как у футбола.
    const isLive = match.status === "live";
    const closed = match.status !== "open" && match.status !== "live";
    const card = el("div", "card" + (isLive ? " live" : ""));

    const top = el("div", "top");
    if (isLive) top.append(liveTag("LIVE", "live"));
    top.append(el("span", "tour", match.short_tournament));
    top.append(el("span", "kick", timeText(match.start_time)));
    card.append(top);

    const board = el("div", "fb-board");
    const teams = el("div", "fb-teams");
    teams.append(document.createTextNode(match.team_a));
    teams.append(el("span", null, "—"));
    teams.append(document.createTextNode(match.team_b));
    board.append(teams);
    if (match.score) board.append(el("div", "fb-score" + (isLive ? " live" : ""), match.score));
    card.append(board);

    const markets = Array.isArray(match.markets) ? match.markets : [];
    const reg = markets.filter((market) => market.key === "reg");
    const final = markets.filter((market) => market.key === "final");
    const rest = markets.filter((market) => market.key !== "reg" && market.key !== "final");

    const row = (name, items, columns) => {
      const grid = el("div", "fb-odds");
      if (columns === 2) grid.style.gridTemplateColumns = "repeat(2, 1fr)";
      items.forEach((market) => {
        const chip = el("button", "odd");
        chip.type = "button";
        chip.append(el("span", "name", market.label));
        chip.append(el("span", "val", oddsText(market.odds)));
        chip.disabled = closed;
        chip.addEventListener("click", () => openAmountSheet(match, sideOfMarket(market, match), null, market));
        grid.append(chip);
      });
      card.append(el("div", "market-title", name));
      card.append(grid);
    };

    if (reg.length) {
      row("Основное время", reg, 3);
    } else if (isLive) {
      // в live роспись закрыта, но кэфы основного времени приходят в самой линии
      const live = el("div", "fb-odds");
      [["a", "П1"], ["x", "X"], ["b", "П2"]].forEach(([side, label]) => {
        const value = sideOdds(match, side);
        const chip = el("button", "odd");
        chip.type = "button";
        chip.append(el("span", "name", label));
        chip.append(el("span", "val", value ? oddsText(value) : "—"));
        chip.disabled = closed || !value;
        chip.addEventListener("click", () => openAmountSheet(match, side));
        live.append(chip);
      });
      card.append(el("div", "market-title", "Основное время"));
      card.append(live);
    }

    if (final.length) row("Победа в матче (с ОТ и буллитами)", final, 2);

    if (isLive) {
      card.append(el("div", "hint live-hint", "Ставки на основное время открыты · роспись — до старта"));
    }

    if (rest.length && match.status === "open") {
      const open = Boolean(state.fbOpen[match.id]);
      const toggle = el("button", "mkt-toggle" + (open ? " open" : ""));
      toggle.type = "button";
      toggle.append(el("span", null, open ? "Свернуть роспись" : "Тоталы и роспись"));
      toggle.append(el("span", "cnt", rest.length + " " + marketsWord(rest.length)));
      toggle.addEventListener("click", () => {
        state.fbOpen[match.id] = !open;
        haptic("light");
        renderMatches();
      });
      card.append(toggle);
      if (open) card.append(renderMarketGroups(match, rest));
    }

    // в экспресс по хоккею: основное время (П1/X/П2) и победа в матче (с ОТ)
    card.append(legAddRow(match, () => hockeyLegVariants(match), closed));

    return card;
  }

  function hockeyLegVariants(match) {
    const markets = Array.isArray(match.markets) ? match.markets : [];
    const variants = [];
    markets.filter((market) => market.key === "reg").forEach((market) => {
      const team = market.param === "1" ? match.team_a
        : market.param === "x" ? "Ничья" : match.team_b;
      variants.push({ side: sideOfMarket(market, match), name: team + " (осн. время)", odds: market.odds });
    });
    markets.filter((market) => market.key === "final").forEach((market) => {
      const team = market.param === "1" ? match.team_a : match.team_b;
      variants.push({ side: sideOfMarket(market, match), name: team + " (с ОТ)", odds: market.odds });
    });
    // в live росписи нет — берём кэфы линии на основное время
    if (!variants.length) {
      [["a", match.team_a], ["x", "Ничья"], ["b", match.team_b]].forEach(([side, team]) => {
        const odd = sideOdds(match, side);
        if (odd) variants.push({ side, name: team + " (осн. время)", odds: odd });
      });
    }
    return variants;
  }

  function renderMarketGroups(match, markets) {
    const box = el("div", "mkt-body");
    const order = [];
    const groups = new Map();
    markets.forEach((market) => {
      if (!groups.has(market.group)) { groups.set(market.group, []); order.push(market.group); }
      groups.get(market.group).push(market);
    });

    order.forEach((name) => {
      const block = el("div", "mkt-group");
      block.append(el("div", "mkt-title", name));
      const grid = el("div", "mkt-grid");
      if (name !== "Точный счёт") {
        // рынки парные (ТБ/ТМ, Ф1/Ф2, Да/Нет): пара должна стоять в одной строке
        grid.style.gridTemplateColumns = "repeat(2, 1fr)";
      }
      groups.get(name).forEach((market) => {
        const chip = el("button", "mkt-chip");
        chip.type = "button";
        chip.append(el("span", "lbl", market.label));
        chip.append(el("span", "val", oddsText(market.odds)));
        chip.addEventListener("click", () =>
          openAmountSheet(match, sideOfMarket(market, match), null, market));
        grid.append(chip);
      });
      block.append(grid);
      box.append(block);
    });
    return box;
  }

  function marketsWord(count) {
    const tail = count % 100;
    if (tail >= 11 && tail <= 14) return "рынков";
    const last = count % 10;
    if (last === 1) return "рынок";
    if (last >= 2 && last <= 4) return "рынка";
    return "рынков";
  }

  // сторона ставки для рынка: f:<ключ>:<параметр> у футбола, h:… у хоккея
  function sideOfMarket(market, match) {
    const prefix = match && match.sport === "hockey" ? "h:" : "f:";
    return prefix + market.key + ":" + market.param;
  }

  // Избранные команды: звёздочка на карточке и фильтр «★ Мои».
  function favoriteStar(match) {
    const names = [match.team_a, match.team_b];
    const btn = el("button", "favstar" + (names.some(isFavorite) ? " on" : ""));
    btn.type = "button";
    btn.textContent = names.some(isFavorite) ? "★" : "☆";
    btn.setAttribute("aria-label",
      names.some(isFavorite) ? "Убрать из избранного" : "Добавить в избранное");
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleFavorite(match.team_a);
      if (match.team_b !== match.team_a && !isFavorite(match.team_a)) {
        // повторный тап по звезде снимает обе команды матча
        state.favorites = state.favorites.filter((name) => name !== match.team_b);
        saveFavorites();
      }
      renderMatches();
      updateFavFilter();
    });
    return btn;
  }

  function updateFavFilter() {
    const chip = $("#favFilter");
    if (!chip) return;
    chip.classList.toggle("on", state.onlyFavorites);
    chip.setAttribute("aria-pressed", state.onlyFavorites ? "true" : "false");
    chip.textContent = state.favorites.length ? `★ Мои · ${state.favorites.length}` : "★ Мои";
  }

  function renderMatches() {
    const host = $("#matches");
    host.textContent = "";
    renderSegments();
    renderSportSegments();
    renderCategorySegments();

    if (!state.user) {
      const card = el("div", "card");
      if (state.authPending) {
        card.append(el("div", "empty", "Подключаемся к Telegram…"));
        card.append(el("div", "skeleton"));
        host.append(card);
        return;
      }
      const message = boot.error && /Откройте/i.test(boot.error)
        ? "Откройте приложение кнопкой в боте — тогда появятся матчи и баланс."
        : (boot.error || "Не удалось авторизоваться. Проверьте подключение и попробуйте снова.");
      card.append(el("div", "empty", message));
      const retry = el("button", "btn", "Обновить");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        retry.textContent = "Подключаемся…";
        await waitForTelegramData(3000);
        await refreshState();
        if (retry.isConnected) {
          retry.disabled = false;
          retry.textContent = "Обновить";
        }
      });
      card.append(retry);
      host.append(card);
      return;
    }

    const liveMode = state.segment === "live";
    const footballMode = state.sport === "football";
    const hockeyMode = state.sport === "hockey";
    const source = liveMode
      ? state.liveMatches
      : (hockeyMode ? state.hockey : (footballMode ? state.football : state.matches));
    const base = source.filter((match) => (match.sport || "cs2") === state.sport);
    // пустое подменю — тупик: если матчи есть в другом, переключаемся туда
    if (hasCategories() && base.length && !base.some(inCategory)) {
      state.category = state.category === "national" ? "clubs" : "national";
      setTournament(null);
      renderCategorySegments();     // подсветка должна совпадать с тем, что показали
    }
    let sportOnly = base.filter(inCategory);
    if (state.onlyFavorites) {
      const mine = sportOnly.filter((match) => isFavorite(match.team_a) || isFavorite(match.team_b));
      if (mine.length) sportOnly = mine;
      else {
        state.onlyFavorites = false;   // избранное опустело — не показываем пустой экран
        updateFavFilter();
      }
    }
    renderTournamentFilter(sportOnly);
    const tournament = selectedTournament();
    const list = tournament
      ? sportOnly.filter((match) => (match.short_tournament || match.tournament) === tournament)
      : sportOnly;

    if (!list.length) {
      if (tournament && sportOnly.length) {
        // турнир выбран, но в нём сейчас пусто — предлагаем снять фильтр
        const card = el("div", "card");
        card.append(el("div", "empty", `В турнире «${tournament}» сейчас нет матчей.`));
        const all = el("button", "btn ghost", "Показать все турниры");
        all.type = "button";
        all.addEventListener("click", () => { setTournament(null); renderMatches(); });
        card.append(all);
        host.append(card);
        return;
      }
      host.append(emptyWithMascot(liveMode
        ? "Прямо сейчас никто не играет.\nМатчи подтягиваются сами — заглядывай."
        : (hasCategories()
          ? `Матчей ${categoryWord()} сейчас нет.\nЗагляни в другое подменю — там линия может быть полнее.`
          : "Сейчас нет матчей с открытыми ставками.\nЛиния обновляется сама — заходи чуть позже.")));
      return;
    }

    for (const match of list) {
      if (hockeyMode) host.append(renderHockeyCard(match));
      else host.append(footballMode ? renderFootballCard(match) : renderCard(match));
    }
  }

  function cartOdds() {
    return state.cart.reduce((acc, leg) => acc * Number(leg.odds), 1);
  }

  function syncCart() {
    // подтягивает в корзину свежие кэфы: если линия сдвинулась — нога подсветится
    if (!state.cart.length) return false;
    const byId = new Map();
    for (const match of state.matches) byId.set(match.id, match);
    for (const match of state.football) byId.set(match.id, match);
    for (const match of state.hockey) byId.set(match.id, match);
    for (const match of state.liveMatches) byId.set(match.id, match);

    let changed = false;
    state.cart.forEach((leg) => {
      const match = byId.get(leg.match_id);
      if (!match) {
        // матч ушёл из линии (рассчитан или отменён) — ставить такой экспресс нельзя
        if (!leg.missing) {
          leg.missing = true;
          changed = true;
        }
        return;
      }
      if (leg.missing) {
        delete leg.missing;
        changed = true;
      }
      const current = Number(oddsForSide(match, leg.side));
      if (!current) return;
      const previous = Number(leg.odds);
      if (current !== previous) {
        leg.odds = current;
        if (leg.base === undefined) leg.base = previous;
        leg.flash = { dir: current > previous ? "up" : "down", at: Date.now() };
        changed = true;
      }
    });
    if (changed) saveCart();
    return changed;
  }

  function renderCartBar() {
    const bar = $("#cartbar");
    const count = state.cart.length;
    $("#tabExpress").classList.toggle("badge", count > 0);
    $("#cartDot").textContent = String(count);
    document.body.classList.toggle("has-cart", count > 0);
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
      card.append(el("div", "empty", "Корзина пуста.\nОткрой матч и жми «В экспресс» — исходов можно брать сколько угодно (минимум два)."));
      host.append(card);
      return;
    }

    const card = el("div", "card");
    state.cart.forEach((leg, index) => {
      const row = el("div", "rowcard leg" + (leg.missing ? " missing" : ""));
      row.style.margin = "0 0 8px";
      const num = el("div", "rank", String(index + 1));
      const grow = el("div", "grow");
      grow.append(el("b", null, leg.team));

      const sub = el("span", null, leg.missing
        ? "матч уже не в линии — убери исход"
        : (leg.tournament || ""));
      if (!leg.missing && leg.base !== undefined && Number(leg.base) !== Number(leg.odds)) {
        // кэф сдвинулся после добавления: показываем, с какого значения
        sub.append(document.createTextNode(" · было "));
        sub.append(el("span", "was", oddsText(leg.base)));
      }
      grow.append(sub);

      const val = el("span", "val", oddsText(leg.odds));
      if (leg.flash && Date.now() - leg.flash.at < 1600) {
        const arrow = el("span", "arrow", leg.flash.dir === "up" ? "▲" : "▼");
        val.classList.add(leg.flash.dir);
        val.append(arrow);
        const flashAt = leg.flash.at;
        setTimeout(() => {
          val.classList.remove("up", "down");
          arrow.remove();
          if (leg.flash && leg.flash.at === flashAt) delete leg.flash;
        }, 1400);
      }

      const del = el("button", "chip", "✕");
      del.type = "button";
      del.addEventListener("click", () => {
        state.cart = state.cart.filter((item) => !(item.match_id === leg.match_id));
        saveCart(); renderExpress(); renderCartBar();
        haptic("light");
      });
      row.append(num, grow, val, del);
      card.append(row);
    });

    const total = cartOdds();
    const summary = el("div", "summary");
    summary.innerHTML = "";
    // только числа — innerHTML безопасен, а текстовая нода показала бы теги как есть
    summary.innerHTML =
      `Исходов: <b>${state.cart.length}</b> · итоговый кэф: <b>${oddsText(total)}</b>`;

    card.append(summary);

    const broken = state.cart.some((leg) => leg.missing);
    if (broken) {
      summary.append(el("div", "hint", "Один из матчей уже не в линии — убери его, иначе экспресс не собрать."));
    }

    const fewLegs = state.cart.length < 2;
    const place = el("button", "btn", fewLegs ? "Добавь ещё один исход" : "Поставить экспресс");
    place.type = "button";
    place.disabled = broken || fewLegs;
    if (!place.disabled) place.addEventListener("click", () => openExpressSheet());
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
        row.append(el("div", "money plus", `Возможная выплата ${money(express.potential)}`));
        host.append(row);
      });
    }
  }

  function renderTop(rows, mine, season) {
    const host = $("#top");
    host.textContent = "";

    // два зачёта: всё время по балансу и неделя по прибыли
    if (season) {
      const seg = el("div", "bets-filters top-seg");
      [
        ["all", "Всё время"],
        ["season", "Неделя"],
      ].forEach(([code, label]) => {
        const chip = el("button", "chip" + (state.topMode === code ? " on" : ""), label);
        chip.onclick = () => {
          if (state.topMode === code) return;
          state.topMode = code;
          loadTop();
        };
        seg.append(chip);
      });
      host.append(seg);
      if (state.topMode === "season") {
        rows = season.top;
        mine = season.me;
        host.append(el("div", "muted top-note", "Счёт недели — чистая прибыль со ставок. Обнуляется в понедельник."));
      }
    }

    if (!rows || !rows.length) {
      host.append(emptyAction("Пока никто не играл — будь первым.", "К матчам",
        () => switchTab("matches")));
      return;
    }
    const medals = ["🥇", "🥈", "🥉"];
    const rowCard = (rank, name, amount, me, format) => {
      const node = el("div", "rowcard" + (me ? " me" : ""));
      node.append(el("div", "rank", medals[rank - 1] || String(rank)));
      const grow = el("div", "grow");
      grow.append(el("b", null, name + (me ? " · вы" : "")));
      grow.append(el("span", null, me ? "это вы" : "игрок"));
      node.append(grow, el("div", "money", (format || money)(amount)));
      return node;
    };
    const signed = state.topMode === "season" ? moneySigned : money;
    rows.forEach((row) => host.append(rowCard(row.rank, row.name, row.balance, row.me, signed)));

    // игрок вне десятки: показываем его место отдельно, чтобы не искать себя глазами
    const inTop = rows.some((row) => row.me);
    if (!inTop && mine && mine.rank) {
      host.append(el("h2", null, "Ваше место"));
      const myScore = state.topMode === "season" ? mine.score : mine.balance;
      host.append(rowCard(mine.rank, (state.user && state.user.name) || "Вы", myScore, true, signed));
    }
  }

  // «· ещё 5 ч 12 мин» — чтобы фрибет не сгорел незаметно
  function ticketLeft(ticket) {
    if (!ticket || !ticket.expires_at) return "";
    const left = Number(ticket.expires_at) - Math.floor(Date.now() / 1000);
    if (left <= 0) return " · истёк";
    return ` · ещё ${waitText(left)}`;
  }

  // Обновляем остаток срока прямо в профиле, пока приложение открыто.
  window.setInterval(() => {
    document.querySelectorAll(".ticket-time[data-expires]").forEach((node) => {
      const expires = Number(node.dataset.expires || 0);
      const left = expires - Math.floor(Date.now() / 1000);
      node.textContent = left <= 0 ? " · истёк" : ` · ещё ${waitText(left)}`;
    });
  }, 60_000);

  function dayLabel(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const yesterday = new Date(now.getTime() - 86400000);
    if (d.toDateString() === now.toDateString()) return "Сегодня";
    if (d.toDateString() === yesterday.toDateString()) return "Вчера";
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "long" });
  }

  function renderHistory(items) {
    const host = $("#history");
    host.textContent = "";
    if (!items || !items.length) {
      host.append(emptyAction("История пуста — сделай первую ставку.", "К матчам",
        () => switchTab("matches")));
      return;
    }
    const card = el("div", "card");
    let day = "";
    items.forEach((item) => {
      // заголовок дня: в длинном списке иначе не понять, где сегодня, а где неделя назад
      const label = dayLabel(item.at);
      if (label !== day) {
        day = label;
        card.append(el("div", "day", label));
      }
      const row = el("div", "rowcard");
      row.style.margin = "0";
      row.style.border = "0";
      row.style.borderBottom = "1px dashed #223143";
      row.style.borderRadius = "0";
      row.style.padding = "10px 0";
      row.style.background = "transparent";
      const grow = el("div", "grow");
      // в базе причина записана строчными («ставка · …»), а строку истории хочется с заглавной
      grow.append(el("b", null, item.reason.charAt(0).toUpperCase() + item.reason.slice(1)));
      const d = new Date(item.at * 1000);
      grow.append(el("span", null, d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })));
      row.append(grow);
      row.append(el("div", "money " + (item.delta >= 0 ? "plus" : "minus"), (item.delta >= 0 ? "+" : "−") + money(Math.abs(item.delta))));
      card.append(row);
    });
    host.append(card);
  }

  function daysWord(count) {
    const tail = count % 100;
    if (tail >= 11 && tail <= 14) return "дней";
    const last = count % 10;
    if (last === 1) return "день";
    if (last >= 2 && last <= 4) return "дня";
    return "дней";
  }

  function achievementsCard() {
    const data = state.achievements;
    if (!data || !data.items) return el("div");
    const box = el("div", "card");
    const head = el("div", "section-title");
    head.append(el("h2", null, "Достижения"));
    head.append(el("span", "tag", `${data.unlocked} из ${data.total}`));
    box.append(head);
    const grid = el("div", "achievements");
    data.items.forEach((item) => {
      const cell = el("div", "ach" + (item.unlocked ? " on" : " locked"));
      cell.append(el("i", null, item.icon));
      cell.append(el("span", null, item.title));
      cell.title = item.desc;
      grid.append(cell);
    });
    box.append(grid);
    const progress = el("div", "hint",
      data.unlocked === data.total
        ? "Все достижения открыты — ты собрал коллекцию."
        : "Открыто " + data.unlocked + " из " + data.total + ". Скрытые — просто серые клетки, без подсказок.");
    box.append(progress);
    return box;
  }

  async function openReferralSheet() {
    openSheet((sheet) => {
      sheet.append(el("h3", null, "Пригласи друга"));
      sheet.append(el("div", "sub", "Бонус прилетает, когда друг сделает первую ставку"));
      const loading = el("div", "hint", "Считаю…");
      sheet.append(loading);
      const cancel = el("button", "btn ghost", "Закрыть");
      cancel.type = "button";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);

      api("/api/referral").then((data) => {
        loading.remove();
        sheet.insertBefore(el("div", "hint",
          `Ты получишь ${money(data.bonus)}, друг — ${money(data.welcome)} на счёт.`), cancel);
        const box = el("div", "refbox");
        const code = el("code", null, data.link || "ссылка появится, когда бот узнает свой @username");
        box.append(code);
        const copy = el("button", "btn small", "Скопировать");
        copy.type = "button";
        copy.disabled = !data.link;
        copy.addEventListener("click", () => {
          if (navigator.clipboard) navigator.clipboard.writeText(data.link).catch(() => {});
          haptic("ok");
          toast("Ссылка скопирована", "ok");
        });
        box.append(copy);
        sheet.insertBefore(box, cancel);

        const share = el("button", "btn", "Поделиться ссылкой");
        share.type = "button";
        share.disabled = !data.link;
        share.addEventListener("click", () => {
          const params = { url: data.link, text: "Заходи в «Добрую лудку» — виртуальные монеты, без реальных ставок" };
          if (hasTelegram() && tg.openTelegramLink) {
            const shareUrl = "https://t.me/share/url?url=" + encodeURIComponent(data.link) +
              "&text=" + encodeURIComponent(params.text);
            tg.openTelegramLink(shareUrl);
          } else if (navigator.share) {
            navigator.share(params).catch(() => {});
          } else {
            toast("Ссылка: " + data.link, "ok");
          }
        });
        sheet.insertBefore(share, cancel);

        sheet.insertBefore(el("div", "hint",
          `Пришло друзей: ${data.invited} · начислено ${money(data.earned)}` +
          (data.pending ? ` · ждут первой ставки: ${data.pending}` : "")), cancel);
      }).catch((err) => {
        loading.textContent = err.message;
      });
    });
  }

  function honestyCard() {
    const box = el("div", "card honesty");
    const head = el("div", "section-title");
    head.append(el("h2", null, "Честность"));
    head.append(el("span", "tag", "открыто"));
    box.append(head);
    const host = el("div", null, "");
    host.append(el("div", "hint", "Считаю…"));
    box.append(host);
    api("/api/honesty").then((data) => {
      host.textContent = "";
      const kv = (label, value, hint) => {
        const row = el("div", "kv");
        row.append(el("span", null, label));
        row.append(el("b", null, value));
        host.append(row);
        if (hint) host.append(el("div", "hint", hint));
      };
      kv("Рулетка", data.roulette.rtp + " %", data.roulette.note);
      kv("Кости", data.dice.rtp + " %", data.dice.note);
      kv("Колесо (платная крутка)",
        data.wheel.rtp + " %",
        `${data.wheel.slots} сектора, ${data.wheel.empty} пустых, ${data.wheel.tickets} с билетом · ` +
        `в среднем ${money(data.wheel.expected)} за крутку по ${money(data.wheel.cost)}`);
      kv("Линия ставок", data.line_rtp + " %", "сколько вернулось игрокам из поставленного");
      host.append(el("div", "hint", data.betting_note));
    }).catch((err) => {
      host.textContent = "";
      host.append(el("div", "hint", err.message));
    });
    return box;
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
    host.append(card);

    // билеты и фрибеты: видно, что они есть, на что их тратить и когда сгорят
    const tickets = state.tickets || [];
    tickets.forEach((ticket) => {
      const single = ticket.kind === "single";
      const row = el("div", "tickets-row" + (single ? " freebet" : ""));
      row.append(document.createTextNode(
        single ? "🎟 Фрибет: " : "🎫 Билет в экспресс: "));
      row.append(el("b", null, money(ticket.amount)));
      row.append(document.createTextNode(
        single ? " — поставь на любой ординар" : " — оплати им экспресс"));
      if (single && ticket.expires_at) {
        const timer = el("span", "ticket-time", ticketLeft(ticket));
        timer.dataset.expires = String(ticket.expires_at);
        row.append(timer);
      }
      host.append(row);
    });

    // серия дней: она же умножает ежедневный бонус — пусть игрок это видит
    const daily = (state.bonus && state.bonus.daily) || null;
    if (daily && daily.streak) {
      const line = el("div", "streak-line");
      line.append(el("span", null, "🔥"));
      line.append(document.createTextNode("Серия "));
      line.append(el("b", null, `${daily.streak} ${daysWord(daily.streak)}`));
      if (daily.bonus_percent > 0) {
        line.append(document.createTextNode(` · бонус +${daily.bonus_percent} %`));
      }
      host.append(line);
    }

    host.append(achievementsCard());

    // итоги: винрейт, ROI, любимый турнир — то, чем игрок хвастается
    host.append(el("h2", null, "Моя статистика"));
    const statsHost = el("div", "stats");
    statsHost.id = "stats";
    statsHost.append(el("div", "skeleton"));
    host.append(statsHost);
    loadStats();

    const friends = el("button", "rowcard");
    friends.type = "button";
    const friendsGrow = el("div", "grow");
    friendsGrow.append(el("b", null, "Пригласи друга"));
    friendsGrow.append(el("span", null, "бонус обоим за первую ставку друга"));
    friends.append(el("span", "chev", "🎁"));
    friends.append(friendsGrow);
    friends.append(el("span", "chev", "›"));
    friends.addEventListener("click", openReferralSheet);
    host.append(friends);

    host.append(honestyCard());

    const gift = el("button", "rowcard");
    gift.type = "button";
    const giftGrow = el("div", "grow");
    giftGrow.append(el("b", null, "Бонусы"));
    giftGrow.append(el("span", null, "ежедневный бонус и колесо фортуны"));
    gift.append(icon("bonus"));
    gift.append(giftGrow);
    gift.append(el("span", "chev", "›"));
    gift.addEventListener("click", () => switchTab("bonus"));
    host.append(gift);

    if (state.bets.length) {
      host.append(el("h2", null, "Ставки в игре"));
      state.bets.forEach((bet) => {
        const row = el("div", "rowcard");
        const grow = el("div", "grow");
        grow.append(el("b", null, bet.team));
        grow.append(el("span", null,
          `${bet.team_a} — ${bet.team_b}` +
          (bet.market_label ? ` · ${bet.market_label}` : "") +
          ` · кэф ${oddsText(bet.odds)}`));
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
      "Экспресс — несколько исходов в одной ставке, кэфы перемножаются. " +
      "Зашли все — выплата по итоговому кэфу, не зашёл хотя бы один — сгорает вся ставка. " +
      "Отменённый матч считается с кэфом 1.0. " +
      "Исходы берутся из всех линий: CS2 — серия, футбол — П1/X/П2, " +
      "хоккей — основное время или победа в матче с ОТ. " +
      "Исходов: от 2, потолка по кэфу нет. " +
      `Минимальная ставка — ${money(state.limits.min_bet)}.`));
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

  // Вместо листа ставки при пустом балансе — объяснение и дорога к бонусам.
  function openNoCoinsSheet(need) {
    openSheet((sheet) => {
      sheet.append(el("h3", null, "Не хватает монет"));
      sheet.append(el("div", "sub",
        `Для ставки нужно минимум ${money(need)}, а на балансе ${money(balance())}. ` +
        "Забери ежедневный бонус или крутни колесо — и возвращайся."));
      const bonus = el("button", "btn", "К бонусам");
      bonus.type = "button";
      bonus.style.marginTop = "12px";
      bonus.addEventListener("click", () => { closeSheet(); switchTab("bonus"); });
      sheet.append(bonus);
      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.style.marginTop = "8px";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  // ------------------------------------------------- сумма ставки: хелперы
  // Единые правила для всех, кто спрашивает «сколько?»: листы ординаров,
  // экспрессов и вкладка «Игры». Ручной ввод — везде, пресеты — только помощь.

  function stakeBounds() {
    return {
      min: Math.max(1, Number((state.limits && state.limits.min_bet) || 10)),
      max: Math.max(1, Number((state.casino && state.casino.max_bet) || 5000)),
    };
  }

  // подсказки: минимум, ×5, ×10, ×50 — всё, что влезает в лимит и в баланс
  function pickPresets(min, cap, count) {
    const limit = count || 4;
    const raw = [min, min * 5, min * 10, min * 50, min * 100, min * 250];
    const list = [];
    raw.forEach((value) => {
      if (value >= min && value <= cap && list.indexOf(value) === -1) list.push(value);
    });
    if (!list.length) list.push(Math.min(cap, min));
    return list.slice(0, limit);
  }

  function clampStake(value, min, max) {
    const number = Math.round(Number(value) || 0);
    if (!number) return min;
    return Math.min(max, Math.max(min, number));
  }

  // Сумма для листа: пресеты + поле «своя сумма» + «Макс».
  // Возвращает { value, ok, set, on } — лист сам решает, что делать с суммой.
  function stakePicker(sheet, opts) {
    const bounds = stakeBounds();
    const cap = Math.max(bounds.min, Math.min(bounds.max, Math.floor(Number(opts.cap === undefined ? balance() : opts.cap) || bounds.min)));
    const min = bounds.min;
    const odds = Number(opts.odds) || 0;
    const handlers = { change: null, submit: null };
    let chosen = clampStake(opts.initial === undefined ? pickPresets(min, cap, 1)[0] : opts.initial, min, cap);

    const summary = el("div", "summary");
    const sumText = el("div");
    summary.append(sumText);
    sheet.append(summary);

    const presets = el("div", "presets");
    const chips = [];
    pickPresets(min, cap, 6).forEach((amount) => {
      const chip = el("button", null, money(amount));
      chip.type = "button";
      chip.dataset.value = String(amount);
      chip.addEventListener("click", () => { set(amount); if (input) input.blur(); haptic("light"); });
      chips.push(chip);
      presets.append(chip);
    });
    const allBtn = el("button", "wide", `💥 Всё · ${money(cap)}`);
    allBtn.type = "button";
    allBtn.dataset.value = String(cap);
    allBtn.addEventListener("click", () => { set(cap); if (input) input.blur(); haptic("light"); });
    chips.push(allBtn);
    presets.append(allBtn);
    sheet.append(presets);

    const custom = el("div", "custom");
    const input = el("input");
    input.type = "number";
    input.inputMode = "numeric";
    input.autocomplete = "off";
    input.min = String(min);
    input.max = String(cap);
    input.placeholder = `своя сумма · от ${min}`;
    input.value = String(chosen);
    input.addEventListener("input", () => {
      const parsed = parseInt(input.value, 10);
      chosen = isNaN(parsed) ? 0 : parsed;
      paint();
    });
    input.addEventListener("blur", () => {
      // клампим только когда игрок закончил печатать — чтобы не мешать вводу
      if (input.value === "") { set(chosen); return; }
      if (chosen !== clampStake(chosen, min, cap)) set(chosen);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        if (handlers.submit) handlers.submit();
      }
    });
    const maxBtn = el("button", "btn small", "Макс");
    maxBtn.type = "button";
    maxBtn.style.flex = "0 0 82px";
    maxBtn.addEventListener("click", () => { set(cap); haptic("light"); });
    custom.append(input, maxBtn);
    sheet.append(custom);

    function set(value) {
      chosen = clampStake(value, min, cap);
      if (input) input.value = String(chosen);
      paint();
    }

    function ok() {
      return chosen >= min && chosen <= cap;
    }

    function paint() {
      sumText.textContent = "";
      const line = el("div");
      const payout = roundPayout(chosen || 0, odds);
      const profit = typeof opts.profit === "function"
        ? Number(opts.profit(chosen || 0, payout))
        : payout - (chosen || 0);
      line.append(document.createTextNode("Ставка: "));
      line.append(el("b", null, chosen ? money(chosen) : "—"));
      line.append(document.createTextNode(" · возможная выплата: "));
      line.append(el("span", "big", odds ? money(payout) : "—"));
      sumText.append(line);
      sumText.append(el("div", "summary-profit",
        "Чистая прибыль: " + (odds ? moneySigned(profit) : "—")));
      if (chosen && chosen < min) {
        sumText.append(el("div", "hint bad", `Минимум — ${money(min)}`));
      } else if (chosen > cap) {
        sumText.append(el("div", "hint bad", `Максимум — ${money(cap)}`));
      }
      chips.forEach((chip) => chip.classList.toggle("chosen", Number(chip.dataset.value) === chosen));
      if (handlers.change) handlers.change(chosen, ok());
    }

    paint();
    return {
      value: () => chosen,
      ok,
      set,
      paint,
      focus: () => input.focus(),
      on: (next) => {
        if (next.change !== undefined) handlers.change = next.change;
        if (next.submit !== undefined) handlers.submit = next.submit;
        paint();
      },
    };
  }

  function openAmountSheet(match, side, map, market) {
    haptic("light");
    if (!canBet()) { haptic("bad"); openNoCoinsSheet(minBet()); return; }
    const team = market ? market.label : teamName(match, side);
    const odd = market
      ? market.odds
      : (map
        ? (side === "a" ? map.odds_a : map.odds_b)
        : sideOdds(match, side));
    const marketTitle = market
      ? `${team} · ${oddsText(market.odds)}`
      : (map ? `Карта ${map.number} · ${team}` : team);
    // один токен на весь лист: повторный тап (или ретрай после обрыва) не спишет дважды
    const token = newToken();
    let body;
    if (market) body = {
      match_id: match.id,
      side,
      market: match.sport === "hockey" ? "hockey" : "football",
    };
    else if (map) body = { match_id: match.id, side, market: "map", map_id: map.id };
    else body = { match_id: match.id, side, market: "match" };

    openSheet((sheet) => {
      sheet.append(el("h3", null, marketTitle));
      sheet.append(el("div", "sub",
        `${match.short_tournament} · ${team} · кэф ${oddsText(odd)} · ${timeText(match.start_time)}`));

      // фрибет за возвращение: ставим его на любой ординар
      const freebet = freebetOf();
      let betByTicket = false;
      let ticketBtn = null;
      if (freebet) {
        ticketBtn = el("button", "btn ghost ticketbtn",
          `🎟 Поставить фрибетом · ${money(freebet.amount)}${ticketLeft(freebet)}`);
        ticketBtn.type = "button";
        ticketBtn.addEventListener("click", () => {
          if (freebet.expires_at && Number(freebet.expires_at) <= Math.floor(Date.now() / 1000)) {
            toast("Срок действия фрибета истёк", "bad");
            refreshState();
            return;
          }
          betByTicket = !betByTicket;
          ticketBtn.classList.toggle("on", betByTicket);
          ticketBtn.textContent = betByTicket
            ? `✓ Ставка фрибетом · ${money(freebet.amount)}`
            : `🎟 Поставить фрибетом · ${money(freebet.amount)}${ticketLeft(freebet)}`;
          if (betByTicket) picker.set(freebet.amount);
          else picker.set(Math.min(Number(freebet.amount), balance()));
          picker.paint();
          haptic("light");
        });
        sheet.append(ticketBtn);
      }

      // с фрибетом потолок — его номинал, даже если на балансе пусто
      const cap = Math.max(balance(), freebet ? Number(freebet.amount) : 0);
      const picker = stakePicker(sheet, {
        odds: odd,
        cap,
        profit: (amount, payout) => betByTicket && freebet && amount === Number(freebet.amount)
          ? payout
          : payout - amount,
      });

      const place = el("button", "btn", "Поставить");
      place.type = "button";
      place.addEventListener("click", async () => {
        const amount = picker.value();
        if (betByTicket) {
          // сумма зафиксирована номиналом фрибета — монеты не проверяем
        } else {
          if (amount < state.limits.min_bet) { toast(`Минимум ${money(state.limits.min_bet)}`, "bad"); return; }
          if (amount > balance()) { toast("Не хватает монет", "bad"); return; }
        }
        place.disabled = true;
        place.textContent = "Отправляю…";
        try {
          const payload = Object.assign({}, body, { amount, token });
          if (betByTicket && freebet) payload.ticket_id = freebet.id;
          const data = await api("/api/bet", { method: "POST", body: payload });
          setBalance(data.balance);
          if (betByTicket && freebet) {
            state.tickets = (state.tickets || []).filter((item) => item.id !== freebet.id);
          }
          closeSheet();
          haptic("ok");
          const profitText = betByTicket
            ? `выплата ${money(data.potential)} · чистая прибыль ${moneySigned(data.potential)}`
            : `выплата ${money(data.potential)} · чистая прибыль ${moneySigned(data.potential - amount)}`;
          toast(`Ставка принята · кэф ${oddsText(data.odds)} · ${profitText}` +
            (betByTicket ? " · по фрибету" : ""), "ok");
          refreshState();
          loadBets();
        } catch (err) {
          haptic("bad");
          toast(err.message, "bad");
          place.disabled = false;
          place.textContent = "Поставить";
        }
      });

      // сумма вне лимитов → кнопка гаснет, Enter в поле сразу ставит
      picker.on({
        change: (amount, valid) => {
          // фрибет фиксированного номинала: поменял сумму — платим монетами
          if (betByTicket && freebet && amount !== Number(freebet.amount)) {
            betByTicket = false;
            if (ticketBtn) {
              ticketBtn.classList.remove("on");
              ticketBtn.textContent =
                `🎟 Поставить фрибетом · ${money(freebet.amount)}${ticketLeft(freebet)}`;
            }
          }
          place.disabled = betByTicket ? false : (!valid || amount > balance());
        },
        submit: () => { if (betByTicket || picker.ok()) place.click(); },
      });
      sheet.append(place);

      // этот же исход можно взять в экспресс, не закрывая лист и не ища карточку
      const alreadyInCart = state.cart.find((leg) => leg.match_id === match.id);
      if (!map && !alreadyInCart) {
        const toExpress = el("button", "btn ghost", "＋ В экспресс");
        toExpress.type = "button";
        toExpress.style.marginTop = "8px";
        toExpress.addEventListener("click", () => {
          const value = market ? market.odds : sideOdds(match, side);
          if (!value) { toast("Кэф уже снят", "bad"); return; }
          state.cart.push({
            match_id: match.id,
            side,
            team: market ? market.label : teamName(match, side),
            odds: value,
            base: value,
            tournament: match.short_tournament,
            sport: match.sport || "cs2",
          });
          saveCart();
          closeSheet();
          renderMatches();
          renderExpress();
          renderCartBar();
          haptic("ok");
          const total = cartOdds();
          toast(`В экспрессе: ${market ? market.label : teamName(match, side)} · кэф ${oddsText(total)}` +
            (state.cart.length < 2 ? " · нужен ещё исход" : ""), "ok");
        });
        sheet.append(toExpress);
      }

      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.style.marginTop = "8px";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  function openExpressSheet() {
    if (state.cart.length < 2) { toast("Нужно минимум 2 исхода", "bad"); return; }
    const expressTicket = (state.tickets || []).some(
      (item) => item.kind === "express" || !item.kind
    );
    if (balance() < minBet() && !expressTicket) {
      haptic("bad"); openNoCoinsSheet(minBet()); return;
    }
    const token = newToken();
    const total = cartOdds();

    openSheet((sheet) => {
      sheet.append(el("h3", null, `Экспресс · ${state.cart.length} исхода`));
      sheet.append(el("div", "sub", `итоговый кэф ${oddsText(total)}`));

      // билет с колеса: ставка за счёт заведения, монеты со счёта не уходят
      const ticket = (state.tickets || []).find(
        (item) => item.kind === "express" || !item.kind
      ) || null;
      let ticketOn = false;
      let ticketBtn = null;
      if (ticket) {
        ticketBtn = el("button", "btn ghost ticketbtn", `🎫 Оплатить билетом · ${money(ticket.amount)}`);
        ticketBtn.type = "button";
        ticketBtn.addEventListener("click", () => {
          ticketOn = !ticketOn;
          ticketBtn.classList.toggle("on", ticketOn);
          ticketBtn.textContent = ticketOn
            ? `✓ Ставка билетом · ${money(ticket.amount)}`
            : `🎫 Оплатить билетом · ${money(ticket.amount)}`;
          if (ticketOn) picker.set(ticket.amount);
          picker.paint();
          haptic("light");
        });
        sheet.append(ticketBtn);
      }

      const picker = stakePicker(sheet, {
        odds: total,
        cap: Math.max(balance(), ticket ? Number(ticket.amount) : 0),
        profit: (amount, payout) => ticketOn && ticket && amount === Number(ticket.amount)
          ? payout
          : payout - amount,
      });

      const place = el("button", "btn", "Поставить экспресс");
      place.type = "button";
      place.addEventListener("click", async () => {
        const amount = picker.value();
        if (!ticketOn) {
          if (amount < state.limits.min_bet) { toast(`Минимум ${money(state.limits.min_bet)}`, "bad"); return; }
          if (amount > balance()) { toast("Не хватает монет", "bad"); return; }
        }
        place.disabled = true;
        place.textContent = "Отправляю…";
        try {
          const body = {
            amount,
            token,
            legs: state.cart.map((leg) => ({ match_id: leg.match_id, side: leg.side })),
          };
          if (ticketOn && ticket) body.ticket_id = ticket.id;
          const data = await api("/api/express", { method: "POST", body });
          setBalance(data.balance);
          if (ticketOn && ticket) state.tickets = (state.tickets || []).filter((item) => item.id !== ticket.id);
          state.cart = [];
          saveCart();
          closeSheet();
          haptic("ok");
          const profitText = ticketOn
            ? `выплата ${money(data.potential)} · чистая прибыль ${moneySigned(data.potential)}`
            : `выплата ${money(data.potential)} · чистая прибыль ${moneySigned(data.potential - amount)}`;
          toast(`Экспресс #${data.express_id} принят · кэф ${oddsText(data.odds)} · ${profitText}` +
            (ticketOn ? " · по билету" : ""), "ok");
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

      picker.on({
        change: (amount, valid) => {
          // билет фиксированного номинала: поменял сумму — платим монетами
          if (ticketOn && ticket && amount !== Number(ticket.amount)) {
            ticketOn = false;
            if (ticketBtn) {
              ticketBtn.classList.remove("on");
              ticketBtn.textContent = `🎫 Оплатить билетом · ${money(ticket.amount)}`;
            }
          }
          place.disabled = ticketOn ? false : !valid;
        },
        submit: () => { if (ticketOn || picker.ok()) place.click(); },
      });
      sheet.append(place);

      const cancel = el("button", "btn ghost", "Отмена");
      cancel.type = "button";
      cancel.style.marginTop = "8px";
      cancel.addEventListener("click", closeSheet);
      sheet.append(cancel);
    });
  }

  function removeLeg(matchId) {
    const before = state.cart.length;
    state.cart = state.cart.filter((leg) => leg.match_id !== matchId);
    if (state.cart.length === before) return;
    saveCart();
    renderMatches();
    renderExpress();
    renderCartBar();
    haptic();
    toast("Убрал из экспресса");
  }

  function addLeg(match, buildVariants) {
    if (state.cart.some((leg) => leg.match_id === match.id)) {
      toast("Этот матч уже в корзине", "bad");
      return;
    }
    const maxLegs = Number(state.limits.max_legs) || 0;
    if (maxLegs && state.cart.length >= maxLegs) {
      toast(`Максимум ${maxLegs} исходов`, "bad");
      return;
    }
    // у футбола и хоккея исходов больше двух, поэтому список вариантов приходит
    // из карточки матча; у CS2 остаются две команды
    const variants = buildVariants ? buildVariants() : [
      { side: "a", name: match.team_a, odds: match.odds_a },
      { side: "b", name: match.team_b, odds: match.odds_b },
    ];
    openSheet((sheet) => {
      sheet.append(el("h3", null, "Какой исход берём?"));
      sheet.append(el("div", "sub", match.short_tournament));
      const pick = (side, name, odd) => {
        const team = name || teamName(match, side);
        state.cart.push({
          match_id: match.id,
          side,
          team,
          odds: odd,     // текущий кэф исхода
          base: odd,     // кэф в момент добавления: показываем «было», если линия сдвинулась
          tournament: match.short_tournament,
          sport: match.sport || "cs2",
        });
        saveCart();
        closeSheet();
        renderMatches();   // карточка сразу показывает «В экспрессе»
        renderExpress();
        renderCartBar();
        haptic("ok");
        const total = cartOdds();
        toast(`Добавлено: ${team} · кэф ${oddsText(total)}` + (state.cart.length < 2 ? " · нужен ещё исход" : ""), "ok");
      };
      variants.forEach((variant) => {
        const btn = el("button", "btn ghost");
        btn.type = "button";
        btn.style.marginBottom = "8px";
        btn.style.justifyContent = "space-between";
        btn.append(el("span", null, variant.name), el("b", null, oddsText(variant.odds)));
        if (!variant.odds) {
          btn.disabled = true;    // кэфа нет: рынок мог закрыться
          sheet.append(btn);
          return;
        }
        btn.addEventListener("click", () => pick(variant.side, variant.name, variant.odds));
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
        if (state.bonus) {
          state.bonus.daily.available = false;
          state.bonus.daily.wait_seconds = data.wait_seconds || 24 * 3600;
        }
        state.teaseTaps = 0;
      } else {
        toast(`Бонус уже получен. Следующий через ${waitText(data.wait_seconds)}`, "bad");
        if (state.bonus) {
          state.bonus.daily.available = false;
          state.bonus.daily.wait_seconds = data.wait_seconds;
        }
      }
      refreshState();
      if (state.tab === "bonus") renderBonus();
      renderBonusDots();
    } catch (err) {
      toast(err.message, "bad");
    }
  }


  // ------------------------------------------------------------- БОНУСЫ
  const WHEEL_SECTOR_DEG = 360 / 24;
  // Палитра по типу приза: золото — джекпот, изумруд — билет, фиолет — крупный,
  // бирюза — средний, сталь — мелкий, графит — пусто.
  const WHEEL_COLORS = {
    jack: ["#FFD75E", "#F0A200", "#2A1C00"],
    ticket: ["#5CF08A", "#17B04B", "#04230F"],
    big: ["#A879FF", "#7B3BFF", "#F3EAFF"],
    mid: ["#2BE3C6", "#0FB6A0", "#04231F"],
    small: ["#3C5068", "#2A3A4E", "#DCE8F4"],
    empty: ["#1F2A37", "#161E28", "#5F7387"],
  };
  const WHEEL_LABELS = {
    jack: "💎",
    ticket: "🎫",
  };

  function svgNode(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) Object.keys(attrs).forEach((key) => node.setAttribute(key, attrs[key]));
    return node;
  }

  function icon(name, cls) {
    const svg = svgNode("svg", { class: cls || "ic", "aria-hidden": "true" });
    const use = svgNode("use", { href: "#i-" + name });
    svg.append(use);
    return svg;
  }

  function polar(cx, cy, radius, angleDeg) {
    // угол считается от «12 часов» по часовой стрелке
    const rad = ((angleDeg - 90) * Math.PI) / 180;
    return [cx + radius * Math.cos(rad), cy + radius * Math.sin(rad)];
  }

  function wheelSvg(slots) {
    const size = 300;
    const cx = size / 2;
    const cy = size / 2;
    const radius = cx - 16;          // сектора
    const svg = svgNode("svg", {
      class: "wheel-svg", viewBox: "0 0 " + size + " " + size,
      role: "img", "aria-label": "Колесо фортуны",
    });
    svg.id = "wheelSvg";
    if (state.wheelAngle) svg.style.transform = `rotate(${state.wheelAngle}deg)`;

    slots.forEach((slot, index) => {
      const from = index * WHEEL_SECTOR_DEG;
      const to = from + WHEEL_SECTOR_DEG;
      const [x1, y1] = polar(cx, cy, radius, from);
      const [x2, y2] = polar(cx, cy, radius, to);
      const [c1, c2] = WHEEL_COLORS[slot.tier] || WHEEL_COLORS.small;
      const gradientId = `wheel-g-${index}`;
      const gradient = svgNode("linearGradient", {
        id: gradientId, gradientUnits: "userSpaceOnUse",
        x1: x1.toFixed(2), y1: y1.toFixed(2), x2: x2.toFixed(2), y2: y2.toFixed(2),
      });
      gradient.append(svgNode("stop", { offset: "0", "stop-color": c1 }));
      gradient.append(svgNode("stop", { offset: "1", "stop-color": c2 }));
      const defs = svgNode("defs");
      defs.append(gradient);
      svg.append(defs);

      const path = svgNode("path", {
        d: `M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${radius} ${radius} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`,
        fill: `url(#${gradientId})`,
        stroke: "#0C1219",
        "stroke-width": "1",
        class: "wheel-sector",
      });
      path.dataset.sector = String(index);
      svg.append(path);

      // подпись держим вертикально: на 24 секторах повёрнутые цифры читаются
      // вверх ногами, а по ширине 3 знака спокойно влезают в сектор
      const [lx, ly] = polar(cx, cy, radius * 0.8, from + WHEEL_SECTOR_DEG / 2);
      const label = svgNode("text", {
        x: lx.toFixed(2),
        y: ly.toFixed(2),
        "text-anchor": "middle",
        "dominant-baseline": "middle",
        "font-size": slot.tier === "jack" ? "12" : "11",
        "font-weight": "800",
        fill: (WHEEL_COLORS[slot.tier] || WHEEL_COLORS.small)[2],
      });
      label.textContent = WHEEL_LABELS[slot.tier]
        || (slot.value === 0 ? "·" : String(slot.value));
      svg.append(label);
    });

    // лампочки по ободу: при вращении по ним бежит «огонёк»
    const lights = svgNode("g", { class: "wheel-lights" });
    for (let index = 0; index < slots.length; index += 1) {
      const [lx, ly] = polar(cx, cy, radius + 7, index * WHEEL_SECTOR_DEG + WHEEL_SECTOR_DEG / 2);
      lights.append(svgNode("circle", {
        cx: lx.toFixed(2), cy: ly.toFixed(2), r: "2.1",
        class: index % 2 === 0 ? "lamp on" : "lamp",
        style: `animation-delay:${(index * 0.05).toFixed(2)}s`,
      }));
    }
    svg.append(lights);

    // обод: металлический блик сверху-слева, тень снизу-справа
    const rimGradient = svgNode("linearGradient", { id: "wheel-rim-g", x1: "0", y1: "0", x2: "1", y2: "1" });
    rimGradient.append(svgNode("stop", { offset: "0", "stop-color": "#9fb3c8" }));
    rimGradient.append(svgNode("stop", { offset: "0.45", "stop-color": "#2b3a4b" }));
    rimGradient.append(svgNode("stop", { offset: "1", "stop-color": "#8496a8" }));
    const rimDefs = svgNode("defs");
    rimDefs.append(rimGradient);
    svg.append(rimDefs);

    svg.append(svgNode("circle", {
      cx: String(cx), cy: String(cy), r: String(radius), class: "wheel-rim",
    }));
    return svg;
  }

  function waitText(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h) return `${h} ч ${m} мин`;
    return `${Math.max(1, m)} мин`;
  }

  function renderBonus() {
    const host = $("#bonus");
    if (!host) return;
    host.textContent = "";

    if (!state.user || !state.bonus) {
      host.append(el("div", "empty", "Открой приложение кнопкой в боте — тогда появятся бонусы."));
      return;
    }

    host.append(dailyBonusCard());
    const chainCard = chainBox();
    if (chainCard) host.append(chainCard);
    const quests = questsCard();
    if (quests) host.append(quests);
    host.append(wheelCard());
    host.append(wheelRulesCard());
  }

  // Задания дня: три штуки, прогресс и кнопка «Забрать», когда выполнено.
  // Цепочка дней: закрыл все задания дня — день в серию, за 3 и 7 дней — сундук
  function chainBox() {
    const chain = state.chain;
    if (!chain) return null;
    const card = el("div", "card");
    const head = el("div", "section-title");
    head.append(el("h2", null, "Серия дней"));
    head.append(el("span", "tag " + (chain.days ? "won" : "open"),
      `${chain.days || 0} ${daysWord(chain.days || 0)}`));
    card.append(head);

    const dots = el("div", "streak-line");
    dots.append(el("span", null, "🔥"));
    dots.append(document.createTextNode("Закрывай все задания дня — серия растёт, сундук жирнеет."));
    card.append(dots);

    if (chain.chest) {
      const ready = (chain.chest_day || 0) < chain.chest.days;
      const btn = el("button", "btn" + (ready ? "" : " ghost"),
        ready ? `Забрать сундук · ${money(chain.chest.reward)}` : `Сундук за ${chain.chest.days} дней уже забран`);
      btn.type = "button";
      btn.disabled = !ready;
      if (ready) btn.addEventListener("click", () => claimChest(btn));
      card.append(btn);
    }
    if (chain.next) {
      const left = chain.next.days - (chain.days || 0);
      card.append(el("div", "hint",
        `Ещё ${left} ${daysWord(left)} — и сундук на ${money(chain.next.reward)}`));
    }
    return card;
  }

  async function claimChest(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const data = await api("/api/quest/chest", { method: "POST" });
      setBalance(data.balance);
      haptic("ok");
      toast(`Сундук: +${money(data.amount)}`, "ok");
      refreshState({ silent: true });
    } catch (err) {
      toast(err.message, "bad");
      if (btn) btn.disabled = false;
    }
  }

  function questsCard() {
    const list = state.quests || [];
    if (!list.length) return null;
    const card = el("div", "card");
    const head = el("div", "section-title");
    head.append(el("h2", null, "Задания дня"));
    const ready = list.filter((quest) => quest.ready).length;
    if (ready) head.append(el("span", "tag won", `готово ${ready}`));
    card.append(head);

    list.forEach((quest) => {
      const row = el("div", "quest" + (quest.claimed ? " done" : quest.ready ? " ready" : ""));
      const grow = el("div", "grow");
      grow.append(el("b", null, quest.title));
      const line = el("div", "hint",
        quest.claimed ? "Награда получена" : `${quest.progress} из ${quest.target} · ${money(quest.reward)}`);
      grow.append(line);
      if (!quest.claimed) {
        const bar = el("div", "qbar");
        const fill = el("i");
        fill.style.width = Math.min(100, Math.round((quest.progress / Math.max(1, quest.target)) * 100)) + "%";
        bar.append(fill);
        grow.append(bar);
      }
      row.append(grow);

      // не готовая награда выглядит как справка, а не как кнопка
      const btn = el("button", "btn small" + (quest.claimed || quest.ready ? "" : " ghost"),
        quest.claimed ? "✓" : quest.ready ? "Забрать" : money(quest.reward));
      btn.type = "button";
      btn.disabled = quest.claimed || !quest.ready;
      if (quest.ready) btn.addEventListener("click", () => claimQuest(quest.code, btn));
      row.append(btn);
      card.append(row);
    });
    return card;
  }

  async function claimQuest(code, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const data = await api("/api/quest", { method: "POST", body: { code } });
      if (data.claimed) {
        setBalance(data.balance);
        haptic("ok");
        toast(`Задание выполнено: +${money(data.reward)}`, "ok");
      } else if (data.error) {
        toast(data.error, "bad");
      }
      state.quests = data.quests || state.quests;
      if (state.tab === "bonus") renderBonus();
      renderBonusDots();
    } catch (err) {
      toast(err.message, "bad");
      if (state.tab === "bonus") renderBonus();
    }
  }

  function dailyBonusCard() {
    const daily = state.bonus.daily;
    const card = el("div", "bonuscard daily");
    card.append(el("div", "glow"));

    const title = el("h3", null, "Ежедневный бонус");
    card.append(title);
    card.append(el("div", "sub",
      daily.available
        ? "Забирай раз в сутки — просто за то, что зашёл."
        : `Следующий через ${waitText(daily.wait_seconds)}.`));

    const amount = el("div", "bonus-amount", money(daily.amount));
    card.append(amount);

    // серия: показываем только когда она есть — иначе лишний шум на карточке
    const streak = Number(daily.streak) || 0;
    if (streak > 0) {
      const streakBox = el("div", "streak");
      streakBox.append(el("b", null, `🔥 ${streak} ${streak === 1 ? "день" : streak < 5 ? "дня" : "дней"} подряд`));
      if (daily.bonus_percent > 0) streakBox.append(el("span", null, `+${daily.bonus_percent} % к бонусу`));
      else streakBox.append(el("span", null, "завтра будет больше"));
      card.append(streakBox);
    } else if (daily.next_streak) {
      card.append(el("div", "hint", "Заберёшь сегодня — начнётся серия, и бонус вырастет."));
    }

    const btn = el("button", "btn bonus" + (daily.available ? "" : " waiting"));
    btn.type = "button";
    btn.style.marginTop = "12px";
    btn.id = "dailyBonusBtn";
    btn.append(icon("bonus"));
    btn.append(document.createTextNode(
      daily.available ? `Забрать ${money(daily.amount)}` : `Через ${waitText(daily.wait_seconds)}`));
    btn.addEventListener("click", onDailyTap);
    card.append(btn);
    return card;
  }

  function wheelCard() {
    const wheel = state.bonus.wheel;
    const cost = Number(wheel.cost) || 0;
    const canPay = !wheel.available && balance() >= cost && cost > 0;
    const card = el("div", "bonuscard wheel");
    card.append(el("div", "glow"));

    card.append(el("h3", null, "Колесо фортуны"));
    card.append(el("div", "sub",
      wheel.available
        ? `Крутка в сутки бесплатная, следующие по ${moneySolid(cost)}. Джекпот ${moneySolid(jackpot())} — в среднем раз на 24 прокрута.`
        : canPay
          ? `Бесплатная на сегодня уже была. Следующая — за ${money(cost)}.`
          : `Бесплатная на сегодня уже была, платная стоит ${money(cost)} — не хватает монет.`));

    const wrap = el("div", "wheelwrap");
    wrap.append(wheelSvg(wheel.slots || []));
    wrap.append(el("div", "wheel-pointer"));
    const burst = el("div", "wheel-burst");
    burst.id = "wheelBurst";
    wrap.append(burst);

    const hub = el("button", "wheel-hub");
    hub.type = "button";
    hub.id = "wheelHub";
    if (wheel.available) {
      hub.append(el("b", null, "Крутить"));
      hub.append(el("span", null, "бесплатно"));
    } else if (canPay) {
      hub.append(el("b", null, "Крутить"));
      hub.append(el("span", null, `${cost} 🪙`));
    } else {
      hub.append(el("b", null, "Ждём"));
      hub.append(el("span", null, waitText(wheel.wait_seconds)));
    }
    hub.disabled = (!wheel.available && !canPay) || state.wheelBusy;
    hub.addEventListener("click", spinWheel);
    wrap.append(hub);
    card.append(wrap);

    const result = el("div", "wheel-result");
    result.id = "wheelResult";
    result.textContent = state.wheelResult || "";
    card.append(result);

    const legend = el("div", "wheel-legend");
    const counts = {};
    (wheel.slots || []).forEach((slot) => {
      const key = `${slot.tier}|${slot.value}`;
      counts[key] = counts[key] || { tier: slot.tier, value: slot.value, n: 0 };
      counts[key].n += 1;
    });
    Object.values(counts)
      .sort((a, b) => b.value - a.value)
      .forEach((slot) => {
        const item = el("span", "lg-" + slot.tier);
        const dot = el("i");
        dot.style.background = WHEEL_COLORS[slot.tier] ? WHEEL_COLORS[slot.tier][0] : "#3C5068";
        const title = slot.tier === "jack" ? `💎 ${money(slot.value)}`
          : slot.tier === "ticket" ? `🎫 билет ${money(slot.value)}`
            : slot.value === 0 ? "пусто" : money(slot.value);
        item.append(dot, document.createTextNode(`${title} · ${slot.n}`));
        legend.append(item);
      });
    card.append(legend);
    return card;
  }

  function wheelRulesCard() {
    const cost = Number(state.bonus.wheel.cost) || 0;
    const card = el("div", "card");
    card.append(el("h2", null, "Как это работает"));
    card.append(el("div", "hint",
      `Ежедневный бонус капает раз в 24 часа. Колесо крутится один раз в сутки бесплатно, ` +
      `дальше каждая крутка стоит ${money(cost)}. Приз определяет сервер: все 24 сектора равновероятны, ` +
      `поэтому джекпот ${money(jackpot())} падает в среднем раз на 24 прокрута, ` +
      "средний приз — около 290 🪙, то есть платная крутка возвращает 96 % поставленного. " +
      "Два сектора — билет в экспресс на 100 🪙: это ставка за счёт заведения, ею можно оплатить любой экспресс. " +
      "Сектора каждый день перемешиваются заново, так что запоминать расположение бесполезно."));
    return card;
  }

  async function spinWheel() {
    if (state.wheelBusy || !state.bonus) return;
    const wheel = state.bonus.wheel;
    const cost = Number(wheel.cost) || 0;
    const free = wheel.available;
    if (!free && !(cost > 0 && balance() >= cost)) {
      toast("Монет не хватает — сначала забери ежедневный бонус", "bad");
      return;
    }
    state.wheelBusy = true;
    const hub = $("#wheelHub");
    if (hub) { hub.disabled = true; hub.querySelector("b").textContent = "Крутим"; }

    try {
      const data = await api("/api/wheel", { method: "POST", body: { paid: !free } });
      if (!data.spun) {
        toast(data.error || "Крутка не прошла", "bad");
        refreshState({ silent: true });
        return;
      }
      const slot = (state.bonus.wheel.slots || [])[data.slot] || null;
      await animateWheel(data.slot, slot);
      setBalance(data.balance);
      haptic(data.prize > 0 || data.ticket ? "ok" : "bad");

      const net = data.prize - (data.free ? 0 : Number(data.cost) || 0);
      if (data.ticket) {
        state.wheelResult = `🎫 Билет в экспресс на ${money(data.ticket.amount)} — уже в профиле`;
        toast(`Билет в экспресс: ${money(data.ticket.amount)}`, "ok");
        if (Array.isArray(state.tickets)) state.tickets.push(data.ticket);
      } else if (data.prize > 0) {
        state.wheelResult = `Выпало ${money(data.prize)} — уже на балансе` +
          (data.free ? "" : ` (крутка за ${money(data.cost)})`);
        toast(`Колесо: ${money(data.prize)}` + (net > 0 ? "" : " · в ноль"), "ok");
      } else {
        state.wheelResult = data.free ? "Пусто. В следующий раз повезёт." : `Пусто. Минус ${money(data.cost)}.`;
      }

      if (data.free) {
        state.bonus.wheel.available = false;
        state.bonus.wheel.wait_seconds = 24 * 3600;
      }
      renderBonusDots();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      state.wheelBusy = false;
      // перерисовываем уже без флага «крутим», иначе кнопка навсегда остаётся неактивной
      if (state.tab === "bonus") renderBonus();
    }
  }

  function animateWheel(slotIndex, slot) {
    const svg = $("#wheelSvg");
    const hub = $("#wheelHub");
    if (!svg) return Promise.resolve();

    // встаём точно на середину сектора, но с «дрожанием»: не всегда в яблочко
    const center = slotIndex * WHEEL_SECTOR_DEG + WHEEL_SECTOR_DEG / 2;
    const jitter = (Math.random() - 0.5) * (WHEEL_SECTOR_DEG - 5);
    const current = state.wheelAngle;
    const turns = 6;
    const target = current + turns * 360 + (((-center - jitter - current) % 360) + 360) % 360;
    const duration = 4600;

    const pointer = document.querySelector(".wheel-pointer");
    const lights = svg.querySelector(".wheel-lights");
    svg.classList.add("spinning");
    if (lights) lights.classList.add("chase");
    if (pointer) pointer.classList.add("ticking");
    if (hub) hub.classList.add("spinning-hub");

    // принудительный reflow: иначе браузер склеит два изменения transform
    void svg.getBoundingClientRect();
    svg.style.transform = `rotate(${target}deg)`;
    state.wheelAngle = target;

    return new Promise((resolve) => setTimeout(() => {
      svg.classList.remove("spinning");
      if (lights) lights.classList.remove("chase");
      if (pointer) {
        pointer.classList.remove("ticking");
        pointer.classList.add("landed");
        setTimeout(() => pointer.classList.remove("landed"), 700);
      }
      if (hub) hub.classList.remove("spinning-hub");

      // вспышка выигравшего сектора + искры
      const sector = svg.querySelector(`[data-sector="${slotIndex}"]`);
      if (sector) {
        sector.classList.add("hit");
        setTimeout(() => sector.classList.remove("hit"), 1400);
      }
      const prize = slot && (slot.value || 0);
      if (prize > 0 || (slot && slot.ticket)) sparks(slot, slotIndex);
      resolve();
    }, duration));
  }

  // Искры из выигравшего сектора: летят по направлению сектора и гаснут.
  function sparks(slot, slotIndex) {
    const host = $("#wheelBurst");
    if (!host) return;
    const gold = slot && slot.tier === "jack";
    const tone = !slot ? "small" : slot.tier;
    const angle = slotIndex * WHEEL_SECTOR_DEG + WHEEL_SECTOR_DEG / 2;
    const count = gold ? 18 : slot && slot.value >= 500 ? 12 : 8;
    for (let index = 0; index < count; index += 1) {
      const dot = el("i", "spark " + tone);
      const spread = (Math.random() - 0.5) * 46;
      const distance = 60 + Math.random() * 70;
      dot.style.setProperty("--dx", `${Math.cos((angle + spread - 90) * Math.PI / 180) * distance}px`);
      dot.style.setProperty("--dy", `${Math.sin((angle + spread - 90) * Math.PI / 180) * distance}px`);
      dot.style.animationDelay = `${Math.random() * 90}ms`;
      host.append(dot);
      setTimeout(() => dot.remove(), 1300);
    }
  }

    function onDailyTap() {
    // десять нажатий подряд по кнопке бонуса = пасхалка (раз в сутки)
    if (!state.bonus) return;
    if (state.bonus.daily.available) {
      claimBonus();
      return;
    }
    state.teaseTaps += 1;
    haptic();
    if (state.teaseTaps >= (state.bonus.wheel.tease_taps || 10)) {
      state.teaseTaps = 0;
      claimTease();
      return;
    }
    if (state.teaseTaps === 1) {
      // первый тап в ожидании: человек должен понять, что кнопка живая, и когда ждать
      toast(`Бонус уже получен — следующий через ${waitText(state.bonus.daily.wait_seconds)}`);
    }
  }

  async function claimTease() {
    try {
      const data = await api("/api/tease", { method: "POST" });
      if (data.claimed) {
        setBalance(data.balance);
        haptic("ok");
        toast("заебал, лови полтос на ход ноги 🩴", "ok");
        state.bonus.tease.available = false;
        state.bonus.tease.wait_seconds = data.wait_seconds || 24 * 3600;
      } else {
        toast(`Уже получал сегодня. Следующий через ${waitText(data.wait_seconds)}`, "bad");
      }
      renderBonus();
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
    if (tab === "me") { renderMe(); }
    if (tab === "express") renderExpress();
    if (tab === "bonus") renderBonus();
    if (tab === "games") { renderGames(); refreshState({ silent: true }); }
    renderBonusDots();   // подсветить конверт, если открыт экран бонусов
    state.teaseTaps = 0;   // «подряд» считается в пределах одного экрана
    haptic("light");
  }

  // ------------------------------------------------------- история ставок
  function betStatusTag(item) {
    if (item.cashed_out) return ["void", "продана"];
    if (item.kind === "express") {
      if (item.status === "open") return ["live", "в игре"];
    }
    if (item.status === "awaiting") return ["live", "ждём результата"];
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
    if (item.status === "open" || item.status === "live" || item.status === "awaiting") {
      right.append(document.createTextNode("Возможная выплата "));
      right.append(el("b", null, money(item.potential)));
    } else {
      const paid = Number(item.payout || 0);
      const net = paid - Number(item.stake || 0);
      if (item.cashed_out) {
        right.append(document.createTextNode("Продана · вернулось "));
        right.append(el("b", null, money(paid)));
      } else if (item.result === "lost") {
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

    // продать ставку до старта: возвращаем 95 % — кнопка живёт только у одиночной
    // ставки на исход матча, пока он не начался
    if (item.kind !== "express" && item.cashout && !item.cashed_out &&
        (item.status === "open") && (item.market || "match") === "match") {
      const sell = el("button", "btn ghost small", `Продать за ${money(item.cashout)}`);
      sell.type = "button";
      sell.addEventListener("click", () => cashoutBet(item, sell));
      card.append(sell);
    }

    // действия внизу карточки: повторить (если матч ещё в линии) и купон картинкой
    const actions = el("div", "bet-actions");

    const share = el("button", "btn ghost small", "Поделиться картинкой");
    share.type = "button";
    const kind = item.kind === "express" ? "express" : "bet";
    share.addEventListener("click", () => shareCoupon(kind, item.id, share));
    actions.append(share);

    // повтор: та же сторона, тот же матч — сумма спрашивается заново
    const matchId = Number(item.match_id || 0);
    const simpleSide = item.side === "a" || item.side === "b" || item.side === "x";
    if (matchId && simpleSide && (item.market || "match") === "match") {
      const again = el("button", "btn ghost small", "Повторить");
      again.type = "button";
      const match = findMatch(matchId);
      if (!match) {
        again.disabled = true;
        again.textContent = "Матч уже не в линии";
      } else {
        again.addEventListener("click", () => {
          const odd = sideOdds(match, item.side);
          if (odd === null || odd === undefined) {
            toast("Кэф на этот исход снят", "bad");
            return;
          }
          openAmountSheet(match, item.side, null, null);
        });
      }
      actions.append(again);
    }
    if (item.kind === "express" && item.legs && item.legs.length) {
      const again = el("button", "btn ghost small", "Повторить экспресс");
      again.type = "button";
      again.addEventListener("click", () => repeatExpress(item, again));
      actions.append(again);
    }

    card.append(actions);
    return card;
  }

  // Повтор экспресса: кладём в корзину те ноги, что ещё в линии
  function repeatExpress(item, button) {
    const legs = state.cart.slice();
    let added = 0;
    let missing = 0;
    (item.legs || []).forEach((leg) => {
      const match = findMatch(leg.match_id);
      if (!match) { missing += 1; return; }
      const value = oddsForSide(match, leg.side);
      if (!value) { missing += 1; return; }
      const label = labelForSide(match, leg.side);
      const index = legs.findIndex((cartLeg) => cartLeg.match_id === match.id);
      const fresh = {
        match_id: match.id,
        side: leg.side,
        team: label,
        odds: value,
        base: value,
        tournament: match.short_tournament,
        sport: match.sport || "cs2",
      };
      if (index >= 0) legs[index] = fresh;
      else legs.push(fresh);
      added += 1;
    });
    if (!added) { toast("Матчи этого экспресса уже не в линии", "bad"); return; }
    state.cart = legs;
    saveCart();
    renderMatches();
    renderExpress();
    renderCartBar();
    haptic("ok");
    toast(`Собрал ${added} ${added === 1 ? "исход" : "исхода"} в корзину` +
      (missing ? ` · ${missing} уже не в линии` : ""), added < 2 ? "bad" : "ok");
    if (button) button.disabled = false;
    if (state.tab !== "express") switchTab("express");
  }

  async function cashoutBet(item, button) {
    if (button) { button.disabled = true; button.textContent = "Продаю…"; }
    try {
      const data = await api("/api/cashout", { method: "POST", body: { bet_id: item.id } });
      setBalance(data.balance);
      closeSheet();
      haptic("ok");
      toast(`Ставка продана: +${money(data.refund)}`, "ok");
      loadBets();
      refreshState({ silent: true });
    } catch (err) {
      haptic("bad");
      toast(err.message, "bad");
      if (button) { button.disabled = false; button.textContent = `Продать за ${money(item.cashout)}`; }
    }
  }

  // Купон картинкой: бот присылает PNG в личку, оттуда его пересылают другу.
  // Если бот недоступен (локальный запуск) — открываем картинку напрямую.
  async function shareCoupon(kind, betId, button) {
    const url = `/api/coupon/${kind}/${betId}.png`;
    if (button) {
      button.disabled = true;
      button.textContent = "Готовлю картинку…";
    }
    try {
      const data = await api("/api/share", { method: "POST", body: { kind: kind, id: betId } });
      toast(data.sent ? "Купон отправил в чат" : "Купон готов — смотри в чате", "ok");
    } catch (err) {
      if (err.status === 503) {
        toast("Чат недоступен — открываю картинку", "bad");
        window.open(url, "_blank");
      } else {
        toast(err.message, "bad");
      }
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "Поделиться картинкой";
      }
    }
  }

  function findMatch(matchId) {
    const pools = [state.matches, state.liveMatches, state.football, state.hockey];
    for (let i = 0; i < pools.length; i += 1) {
      const found = (pools[i] || []).find((match) => Number(match.id) === Number(matchId));
      if (found) return found;
    }
    return null;
  }

  function renderBets(data) {
    const host = $("#bets");
    host.textContent = "";
    const open = (data && data.open) || [];
    const settled = (data && data.settled) || [];
    const view = ["all", "open", "settled"].includes(state.betsView) ? state.betsView : "all";

    const tabs = el("div", "segments bets-main-tabs");
    [
      { code: "all", label: "Все", count: open.length + settled.length },
      { code: "open", label: "В игре", count: open.length },
      { code: "settled", label: "Рассчитанные", count: settled.length },
    ].forEach((option) => {
      const button = el("button", "seg" + (view === option.code ? " active" : ""));
      button.type = "button";
      button.setAttribute("aria-pressed", String(view === option.code));
      button.append(document.createTextNode(option.label), el("span", "bets-main-count", String(option.count)));
      button.addEventListener("click", () => {
        state.betsView = option.code;
        haptic("light");
        renderBets(data);
      });
      tabs.append(button);
    });
    host.append(tabs);

    const appendSection = (title, items, emptyText) => {
      if (!items.length) {
        if (emptyText) host.append(emptyWithMascot(emptyText));
        return;
      }
      const sectionTitle = el("div", "section-title");
      sectionTitle.append(el("h2", null, title), el("span", "tag", String(items.length)));
      host.append(sectionTitle);
      items.forEach((item) => host.append(renderBetCard(item)));
    };

    if (view === "open") {
      appendSection("В игре", open, "Ставок в игре пока нет.");
      return;
    }
    if (view === "all") {
      if (!open.length && !settled.length) {
        host.append(emptyWithMascot("Ставок пока нет. Матчи → кэф → сумма."));
        return;
      }
      appendSection("В игре", open);
      appendSection("Рассчитанные", settled);
      return;
    }

    appendSection("Рассчитанные", settled, "Рассчитанных ставок пока нет.");
  }

  // Рассчитанные ставки, которые игрок ещё не видел: говорим тостом,
  // чтобы выигрыш не «появлялся молча» из ниоткуда.
  function notifySettled(data) {
    const settled = (data && data.settled) || [];
    if (!settled.length) return;
    let seen = [];
    try { seen = JSON.parse(localStorage.getItem("cs2bets_seen") || "[]"); } catch (e) { seen = []; }
    const ids = settled.map((item) => String(item.id));
    // проданные ставки игрок только что оформил сам — тост про них лишний
    const fresh = settled.filter((item) => !item.cashed_out && seen.indexOf(String(item.id)) === -1);
    try { localStorage.setItem("cs2bets_seen", JSON.stringify(ids.slice(0, 200))); } catch (e) { /* нет места */ }
    if (!fresh.length || !state.seenOnce) return;   // первый запуск — молча, без шквала тостов
    const net = fresh.reduce((sum, item) => sum + (Number(item.payout || 0) - Number(item.stake || 0)), 0);
    const wins = fresh.filter((item) => item.result === "won").length;
    if (fresh.length === 1) {
      const item = fresh[0];
      const delta = Number(item.payout || 0) - Number(item.stake || 0);
      toast(item.result === "won"
        ? `Ставка зашла: +${money(Math.max(0, delta))}`
        : `Ставка рассчитана: ${item.result === "void" ? "возврат" : "мимо"}`, item.result === "won" ? "ok" : "bad");
    } else {
      toast(`${fresh.length} ставки рассчитаны: ${wins} зашло, итог ${net >= 0 ? "+" : "−"}${money(Math.abs(net))}`,
        net >= 0 ? "ok" : "bad");
    }
  }

  async function loadBets() {
    const host = $("#bets");
    if (!host) return;
    try {
      const data = await api("/api/bets");
      state.betsHistory = data;
      notifySettled(data);
      state.seenOnce = true;
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
      renderTop(data.top, data.me, data.season);
    } catch (err) {
      $("#top").textContent = "";
      $("#top").append(el("div", "empty", err.message));
    }
  }

  function renderStats(stats) {
    const host = $("#stats");
    if (!host || !stats) return;
    host.textContent = "";
    const cells = [
      ["Винрейт", stats.settled ? stats.winrate + " %" : "—", "зашло " + stats.won + " из " + stats.settled],
      ["Итог", (stats.roi > 0 ? "+" : stats.roi < 0 ? "−" : "") + Math.abs(stats.roi) + " %", "ROI по всем ставкам"],
      ["Оборот", money(stats.staked), "поставлено всего"],
      ["Лучшая выплата", stats.biggest_win ? money(stats.biggest_win) : "—", "самая крупная"],
      ["Раундов в играх", String(stats.casino_rounds),
        stats.casino_net >= 0 ? "+" + money(stats.casino_net) : "−" + money(Math.abs(stats.casino_net))],
    ];
    cells.forEach(([label, value, hint]) => {
      const cell = el("div", "stat");
      cell.append(el("span", null, label));
      cell.append(el("b", null, value));
      if (hint) cell.append(el("i", null, hint));
      host.append(cell);
    });
    if (stats.favourite_tournament) {
      const fav = el("div", "stat wide");
      fav.append(el("span", null, "Любимый турнир"));
      fav.append(el("b", null, stats.favourite_tournament));
      host.append(fav);
    }
    if (!stats.bets) {
      host.textContent = "";
      host.append(el("div", "empty", "Пока нет данных: сделай первую ставку — и здесь появится статистика."));
    }
  }

  async function loadStats() {
    const host = $("#stats");
    if (!host) return;
    try {
      const data = await api("/api/stats");
      state.stats = data.stats;
      renderStats(data.stats);
    } catch (err) {
      host.textContent = "";
      host.append(el("div", "empty", err.message));
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
      const football = data.football || [];
      const hockey = data.hockey || [];
      const matchesJson = JSON.stringify([data.matches, liveMatches, football, hockey]);
      const matchesChanged = matchesJson !== state.matchesJson;
      const balanceBefore = state.user ? state.user.balance : null;

      state.user = data.user;
      state.authPending = false;
      state.matches = data.matches;
      state.football = football;
      state.hockey = hockey;
      state.liveMatches = liveMatches;
      state.matchesJson = matchesJson;
      state.bets = data.bets;
      state.expresses = data.expresses;
      state.limits = data.limits;
      state.bonus = data.bonus || null;
      state.quests = data.quests || state.quests;
      state.chain = data.chain || state.chain;
      state.achievements = data.achievements || state.achievements;
      state.tickets = Array.isArray(data.tickets) ? data.tickets : (state.tickets || []);
      state.casino = data.casino || state.casino;
      state.blackjack = data.blackjack || null;
      state.ladder = data.ladder || null;
      state.crash = data.crash || null;
      // новое достижение — единственный тост, который приятно получить неожиданно
      (data.new_achievements || []).forEach((item, index) => {
        setTimeout(() => toast(`${item.icon} Достижение: ${item.title}`, "ok"), index * 2600);
      });
      if (state.casino && !(state.casinoStake.roulette > 0)) state.casinoStake.roulette = 50;
      state.live = true;

      renderHeader();
      if (balanceBefore !== null && data.user && Number(data.user.balance) !== Number(balanceBefore)) {
        setBalance(data.user.balance);   // выплата прилетела, пока экран открыт
      }
      syncCart();  // кэфы в корзине экспресса живут вместе с линией
      if (matchesChanged) renderMatches();  // без изменений не перерисовываем: не мешаем читать
      renderCartBar();
      if (state.tab === "express") renderExpress();
      if (state.tab === "me") renderMe();
      if (state.tab === "bonus") renderBonus();
      if (state.tab === "games" && !state.casinoBusy && !state.casinoTyping && !state.blackjackBusy && !state.blackjackTyping && !state.crashBusy) renderGames();
    } catch (err) {
      if (err.status === 401) {
        state.authPending = false;
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

  // =============================================================== игры
  // Рулетка и кости. Исход бросает сервер (/api/casino/play), клиент получает
  // готовое число и «догоняет» его анимацией: колесо доворачивается до нужной
  // лунки, кубики падают нужной гранью. Подкрутить результат из браузера нельзя.

  const ROULETTE_ORDER = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5,
    24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
  ];
  const ROULETTE_RED = new Set([
    1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36,
  ]);
  const ROULETTE_STEP = 360 / 37;

  // поворот куба, при котором нужная грань смотрит на игрока (в лицо, не вверх):
  // камера смотрит почти прямо, поэтому читается именно лицевая грань.
  // 1 — передняя, 6 — задняя, 3 — правая, 4 — левая, 2 — верхняя, 5 — нижняя.
  const DIE_ORIENT = {
    1: { x: 0, y: 0 },
    2: { x: -90, y: 0 },
    3: { x: 0, y: -90 },
    4: { x: 0, y: 90 },
    5: { x: 90, y: 0 },
    6: { x: 0, y: 180 },
  };
  // точки на гранях: номера клеток сетки 3×3
  const DIE_PIPS = { 1: [5], 2: [1, 9], 3: [1, 5, 9], 4: [1, 3, 7, 9], 5: [1, 3, 5, 7, 9], 6: [1, 3, 4, 6, 7, 9] };

  function rouletteColour(number) {
    if (Number(number) === 0) return "green";
    return ROULETTE_RED.has(Number(number)) ? "red" : "black";
  }

  function casinoLimits() {
    const limits = state.limits || {};
    const max = Number((state.casino && state.casino.max_bet) || 5000);
    return { min: Math.max(1, Number(limits.min_bet) || 10), max };
  }

  function stakeFor(game) {
    const { min, max } = casinoLimits();
    return clampStake(state.casinoStake[game], min, max);
  }

  // что игрок реально ввёл и куда это годится
  function stakeState(game) {
    const { min, max } = casinoLimits();
    const raw = Math.round(Number(state.casinoStake[game]) || 0);
    return {
      min,
      max,
      raw,
      value: clampStake(raw, min, max),
      low: raw < min,
      high: raw > max,
      poor: raw > balance(),
    };
  }

  function stakeOk(game) {
    const info = stakeState(game);
    return !info.low && !info.high && !info.poor;
  }

  // Обновляет только сумму: поле, пресеты, подпись и кнопку. Колесо и кубики
  // не перерисовываются — иначе ввод с клавиатуры терял бы фокус.
  function syncStakeUI(game, options) {
    const info = stakeState(game);
    const keepInput = Boolean(options && options.keepInput);
    const input = $("#stakeInput");
    if (input && !keepInput && document.activeElement !== input) {
      input.value = String(info.raw || info.min);
    }

    document.querySelectorAll("#games .stake-chip").forEach((chip) => {
      chip.classList.toggle("active", Number(chip.dataset.stake) === info.raw);
    });
    const minus = document.querySelector('#games .stake-step[data-step="-1"]');
    if (minus) minus.disabled = state.casinoBusy || info.raw <= info.min;
    const plus = document.querySelector('#games .stake-step[data-step="1"]');
    if (plus) plus.disabled = state.casinoBusy || info.raw >= info.max;

    const odds = potential(game).odds;
    const meta = $("#betMeta");
    if (meta) {
      meta.textContent = "";
      meta.classList.toggle("warn", !stakeOk(game));
      meta.append(el("span", null, "Ставка: " + money(info.raw || 0)));
      const payout = odds ? roundPayout(info.raw || 0, odds) : null;
      meta.append(el("span", null,
        "кэф ×" + (odds ? odds.toFixed(2) : "—") + " · выплата " +
        (payout === null ? "—" : money(payout)) + " · чистая прибыль " +
        (payout === null ? "—" : moneySigned(payout - (info.raw || 0)))));
    }

    const play = $("#playBtn");
    if (play) {
      const ok = stakeOk(game);
      play.disabled = state.casinoBusy || !ok;
      if (info.low) play.textContent = `Минимум — ${money(info.min)}`;
      else if (info.high) play.textContent = `Максимум — ${money(info.max)}`;
      else if (info.poor) play.textContent = "Не хватает монет";
      else play.textContent = (game === "roulette" ? "Крутить колесо" : "Бросить кости") + " · " + money(info.raw);
    }
  }

  function pickFor(game) {
    if (state.casinoPick[game]) return state.casinoPick[game];
    return game === "roulette" ? "red" : "over";
  }

  // ------------------------------------------------------------------ колесо
  function rouletteSvgNode() {
    const size = 300;
    const cx = size / 2;
    const cy = size / 2;
    const radius = 136;
    const svg = svgNode("svg", {
      class: "roulette-svg",
      viewBox: `0 0 ${size} ${size}`,
      role: "img",
      "aria-label": "Колесо рулетки",
    });
    svg.id = "rouletteSvg";
    if (state.rouletteAngle) svg.style.transform = `rotate(${state.rouletteAngle}deg)`;

    svg.append(svgNode("circle", { cx, cy, r: radius + 10, fill: "#0d141c" }));

    ROULETTE_ORDER.forEach((number, index) => {
      const from = index * ROULETTE_STEP;
      const to = from + ROULETTE_STEP;
      const [x1, y1] = polar(cx, cy, radius, from);
      const [x2, y2] = polar(cx, cy, radius, to);
      const colour = rouletteColour(number);
      const fill = colour === "green" ? "#2f8a4c" : colour === "red" ? "#bf3b3b" : "#1d2733";
      svg.append(svgNode("path", {
        d: `M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${radius} ${radius} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`,
        fill,
        stroke: "#0c1219",
        "stroke-width": "1",
      }));
      const [lx, ly] = polar(cx, cy, radius * 0.83, from + ROULETTE_STEP / 2);
      const label = svgNode("text", {
        x: lx.toFixed(2),
        y: ly.toFixed(2),
        "text-anchor": "middle",
        "dominant-baseline": "middle",
        "font-size": "11.5",
        "font-weight": "800",
        fill: "#f6fafd",
        transform: `rotate(${(from + ROULETTE_STEP / 2).toFixed(2)} ${lx.toFixed(2)} ${ly.toFixed(2)})`,
      });
      label.textContent = String(number);
      svg.append(label);
    });

    svg.append(svgNode("circle", {
      cx, cy, r: radius + 1.5, fill: "none", stroke: "#c9a54c", "stroke-width": "2.6", opacity: ".92",
    }));
    svg.append(svgNode("circle", {
      cx, cy, r: radius + 6.5, fill: "none", stroke: "#28394a", "stroke-width": "5",
    }));
    svg.append(svgNode("circle", {
      cx, cy, r: radius * 0.52, fill: "none", stroke: "rgba(255,255,255,.06)", "stroke-width": "9",
    }));
    return svg;
  }

  function rouletteHubNode(number) {
    const hub = el("div", "roulette-hub");
    hub.id = "rouletteHub";
    if (number === null || number === undefined) {
      hub.append(el("b", null, "—"), el("small", null, "ставь"));
    } else {
      const colour = rouletteColour(number);
      hub.classList.add(colour);
      hub.append(
        el("b", null, String(number)),
        el("small", null, colour === "green" ? "зеро" : colour === "red" ? "красное" : "чёрное"),
      );
    }
    return hub;
  }

  function numberStrip() {
    const strip = el("div", "numstrip");
    strip.id = "numStrip";
    const recent = state.recentNumbers || [];
    if (!recent.length) {
      const hint = el("span", "nb ghost", "здесь будут выпавшие числа");
      hint.style.opacity = ".45";
      hint.style.fontWeight = "600";
      hint.style.fontSize = "11px";
      strip.append(hint);
      return strip;
    }
    recent.slice(0, 10).forEach((number) => {
      const pill = el("span", "nb " + rouletteColour(number), String(number));
      strip.append(pill);
    });
    return strip;
  }

  function pushNumber(number) {
    state.recentNumbers = [number].concat(state.recentNumbers || []).slice(0, 10);
    const strip = $("#numStrip");
    if (!strip) return;
    const ghost = strip.querySelector(".nb.ghost");
    if (ghost) ghost.remove();
    const pill = el("span", "nb fresh " + rouletteColour(number), String(number));
    while (strip.children.length >= 10) strip.lastChild.remove();
    strip.insertBefore(pill, strip.firstChild);
  }

  // ------------------------------------------------------------------- кости
  function dieNode(id, dice) {
    const die = el("div", "die");
    die.id = id;
    const cube = el("div", "die-cube");
    const angles = state.diceAngles[dice] || { x: -18, y: 22 };
    cube.style.transform = `rotateX(${angles.x}deg) rotateY(${angles.y}deg)`;
    [1, 2, 3, 4, 5, 6].forEach((value) => {
      const face = el("div", "face s" + value);
      const pips = DIE_PIPS[value];
      if (pips.length <= 2) face.classList.add("edge");
      pips.forEach((cell) => {
        const pip = el("span", "pip");
        pip.style.gridRow = String(Math.floor((cell - 1) / 3) + 1);
        pip.style.gridColumn = String(((cell - 1) % 3) + 1);
        face.append(pip);
      });
      cube.append(face);
    });
    die.append(cube);
    return die;
  }

  function diceStageNode() {
    const stage = el("div", "dice-stage");
    // камера чуть сверху: игрок должен видеть именно ту грань, которая легла вверх
    const cam = el("div", "dice-cam");
    cam.append(dieNode("dieA", 0), dieNode("dieB", 1));
    stage.append(cam);
    return stage;
  }

  // ---------------------------------------------------------------- поле
  function optionGroups(game) {
    const options = (state.casino && state.casino.options && state.casino.options[game]) || [];
    const groups = [];
    options.forEach((option) => {
      if (String(option.code).indexOf("straight:") === 0) return;   // числа живут в отдельной сетке
      const name = option.group || "Ставки";
      let bucket = groups.find((item) => item.name === name);
      if (!bucket) { bucket = { name, items: [] }; groups.push(bucket); }
      bucket.items.push(option);
    });
    return groups;
  }

  function betChip(option, active) {
    const chip = el("button", "bet-chip" + (active ? " active" : ""));
    chip.type = "button";
    chip.dataset.code = option.code;
    if (option.code === "red") chip.classList.add("redded");
    if (option.code === "black") chip.classList.add("blackded");
    chip.append(el("span", "lbl", option.label), el("span", "val", "×" + Number(option.odds).toFixed(2)));
    return chip;
  }

  function numberBoard(pick) {
    const board = el("div", "numboard");
    const zero = el("button", "zero green" + (pick === "straight:0" ? " active" : ""), "0");
    zero.type = "button";
    zero.dataset.code = "straight:0";
    board.append(zero);
    for (let number = 1; number <= 36; number += 1) {
      const cell = el("button", rouletteColour(number) + (pick === "straight:" + number ? " active" : ""), String(number));
      cell.type = "button";
      cell.dataset.code = "straight:" + number;
      board.append(cell);
    }
    return board;
  }

  // Сумма раунда: пресеты-чипы + поле, куда можно вписать любую сумму.
  function stakeRow(game) {
    const { min, max } = casinoLimits();
    const cap = Math.max(min, Math.min(max, balance()));
    const info = stakeState(game);
    const block = el("div", "stake-block");

    const presets = el("div", "stake-presets");
    const cells = pickPresets(min, max, 4).map((value) => ({ value, label: money(value) }));
    if (cap > cells[cells.length - 1].value) cells.push({ value: cap, label: "Всё" });
    cells.forEach((cell) => {
      const chip = el("button", "stake-chip" + (cell.value === info.raw ? " active" : ""), cell.label);
      chip.type = "button";
      chip.dataset.stake = String(cell.value);
      presets.append(chip);
    });
    presets.style.gridTemplateColumns = "repeat(" + cells.length + ",1fr)";

    const row = el("div", "stake-row");
    const minus = el("button", "stake-step", "−");
    minus.type = "button";
    minus.dataset.step = "-1";
    minus.disabled = info.raw <= min;
    const plus = el("button", "stake-step", "+");
    plus.type = "button";
    plus.dataset.step = "1";
    plus.disabled = info.raw >= max;

    const input = el("input", "stake-input");
    input.id = "stakeInput";
    input.type = "number";
    input.inputMode = "numeric";
    input.autocomplete = "off";
    input.min = String(min);
    input.max = String(max);
    input.value = String(info.raw || min);
    input.setAttribute("aria-label", "Сумма ставки");
    input.addEventListener("focus", () => { state.casinoTyping = true; });
    input.addEventListener("input", () => {
      const parsed = parseInt(input.value, 10);
      state.casinoStake[game] = isNaN(parsed) ? 0 : parsed;
      syncStakeUI(game, { keepInput: true });
    });
    input.addEventListener("blur", () => {
      state.casinoTyping = false;
      const raw = Number(state.casinoStake[game]) || 0;
      state.casinoStake[game] = clampStake(raw, min, max);
      input.value = String(state.casinoStake[game]);
      syncStakeUI(game);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    });

    row.append(minus, input, plus);
    block.append(presets, row);
    return block;
  }

  function potential(game) {
    const pick = pickFor(game);
    const options = (state.casino && state.casino.options && state.casino.options[game]) || [];
    const option = options.find((item) => item.code === pick);
    const odds = option ? Number(option.odds) : 0;
    const stake = stakeFor(game);
    return {
      label: option ? option.label : "—",
      odds,
      stake,
      payout: odds ? roundPayout(stake, odds) : null,
      profit: odds ? roundPayout(stake, odds) - stake : null,
    };
  }

  function tableHead(game) {
    const panel = el("div", "casino-panel");
    panel.append(el("div", "felt-glow"));

    if (game === "roulette") {
      const wrap = el("div", "roulette-wrap");
      wrap.append(rouletteSvgNode());
      const ball = el("div", "roulette-ball");
      ball.id = "rouletteBall";
      if (state.ballAngle) ball.style.transform = `rotate(${state.ballAngle}deg)`;
      const dot = el("i");
      ball.append(dot);
      wrap.append(ball);
      wrap.append(el("div", "roulette-pointer"));
      const mine = state.casinoResults.roulette;
      const last = mine ? mine.number : (state.recentNumbers || [])[0];
      wrap.append(rouletteHubNode(last === undefined ? null : last));
      panel.append(wrap);
      panel.append(numberStrip());
      return panel;
    }

    const wrap = el("div", "dice-wrap");
    wrap.append(diceStageNode());
    const sum = el("div", "dice-sum");
    sum.id = "diceSum";
    const shown = state.diceShown;
    sum.textContent = shown ? `сумма ${shown[0] + shown[1]}` : "бросай";
    wrap.append(sum);
    panel.append(wrap);
    return panel;
  }

  function outcomeNode() {
    const box = el("div", "casino-outcome");
    box.id = "casinoOutcome";
    return box;
  }

  // «что выпало» одной строкой: нужно и в блоке результата, и в тосте,
  // когда игрок уже ушёл на другую вкладку
  function outcomeText(result) {
    if (!result) return "";
    if (result.game === "roulette") {
      const colour = rouletteColour(result.number);
      const word = colour === "green" ? "зеро" : colour === "red" ? "красное" : "чёрное";
      return `${result.number} — ${word}`;
    }
    return `${result.dice[0]} + ${result.dice[1]} = ${result.total}`;
  }

  function paintOutcome(result, options) {
    const box = $("#casinoOutcome");
    if (!box) return;
    if (!result) {
      box.className = "casino-outcome";
      box.textContent = "";
      return;
    }
    const pop = options && options.pop;
    box.className = "casino-outcome show " + (result.win ? "win" : "lose") + (pop ? " pop" : "");
    box.textContent = "";

    const what = outcomeText(result);

    const line1 = el("div", "line1");
    line1.append(el("span", null, "Выпало"));
    line1.append(el("span", "big", what));
    const line2 = el("div", "line2", result.win
      ? `«${result.label}» зашла · выплата ${money(result.payout)} · чистая прибыль ${moneySigned(result.payout - result.amount)} (кэф ${Number(result.odds).toFixed(2)})`
      : `«${result.label}» не зашла: ${money(result.amount)} ушли банку`);
    box.append(line1, line2);
  }

  function summaryNode() {
    const summary = (state.casino && state.casino.summary) || { rounds: 0, staked: 0, paid: 0, net: 0, wins: 0 };
    const wrap = el("div", "casino-summary");
    wrap.id = "casinoSummary";
    const cells = [
      ["Раундов", String(summary.rounds)],
      ["Поставлено", money(summary.staked)],
      ["Итог", (summary.net > 0 ? "+" : summary.net < 0 ? "−" : "") + money(Math.abs(summary.net) || 0)],
    ];
    cells.forEach((cell, index) => {
      const box = el("div");
      box.append(el("span", null, cell[0]));
      const value = el("b", index === 2 ? (summary.net > 0 ? "up" : summary.net < 0 ? "down" : "") : "", cell[1]);
      box.append(value);
      wrap.append(box);
    });
    return wrap;
  }

  function historyNode() {
    const host = el("div", "casino-hist");
    host.id = "casinoHist";
    const rows = (state.casino && state.casino.history) || [];
    if (!rows.length) {
      host.append(el("div", "empty", "Пока пусто — первый раунд впереди."));
      return host;
    }
    rows.slice(0, 8).forEach((row) => {
      const net = Number(row.payout) - Number(row.amount);
      const line = el("div", "row " + (net > 0 ? "win" : net < 0 ? "lose" : "push"));
      const who = el("div", "who");
      who.append(document.createTextNode(row.label));
      who.append(el("i", null, row.game === "roulette" ? "рулетка"
        : row.game === "ladder" ? "лесенка"
          : row.game === "blackjack" ? "блэкджек" : row.game === "crash" ? "самолётик" : "кости"));
      const tagText = row.game === "roulette" ? "№ " + row.outcome
        : row.game === "ladder" || row.game === "blackjack" || row.game === "crash" ? row.outcome : "сумма " + row.outcome;
      const tag = el("span", "tag", tagText);
      const res = el("div", "res", (net > 0 ? "+" : net < 0 ? "−" : "") + money(Math.abs(net)));
      line.append(who, tag, res);
      host.append(line);
    });
    return host;
  }

  function rulesNode(game) {
    const box = el("div", "casino-rules");
    if (game === "roulette") {
      box.innerHTML = "";
      box.append(el("b", null, "Рулетка · европейская, 37 лунок. "));
      box.append(document.createTextNode(
        "Красное/чёрное, чёт/нечет, 1–18/19–36 платят ×2, дюжины и колонки — ×3, точное число — ×36. " +
        "Зеро забирает равные ставки."
      ));
      box.append(el("span", "edge", "преимущество банка 2.7 %"));
    } else {
      box.append(el("b", null, "Кости · два кубика. "));
      box.append(document.createTextNode(
        "«Меньше 6» — сумма 2–6, «больше 8» — сумма 8–12 (по ×2.29), точная сумма — от ×5.73 за семёрку до ×34.38 за дубль."
      ));
      box.append(el("span", "edge", "преимущество банка 4.5 %"));
    }
    return box;
  }

  function crashReadout() {
    const game = state.crash;
    if (!game || game.status !== "flying") return 1;
    const elapsed = Math.max(0, Date.now() / 1000 - Number(game.started_at));
    const raw = elapsed >= Math.log(1_000_000) / 0.16 ? 1_000_000 : Math.exp(0.16 * elapsed);
    return Math.floor(raw * 100) / 100;
  }

  function crashFlightScene(withPlane) {
    const svg = svgNode("svg", {
      class: "crash-flight-svg", viewBox: "0 0 360 220", preserveAspectRatio: "none",
      "aria-hidden": "true",
    });
    const defs = svgNode("defs");
    const gradient = svgNode("linearGradient", { id: "crash-flying-plane-g", x1: "0", y1: "0", x2: "1", y2: "1" });
    gradient.append(
      svgNode("stop", { offset: "0", "stop-color": "#fff3d1" }),
      svgNode("stop", { offset: ".55", "stop-color": "#d7c18b" }),
      svgNode("stop", { offset: "1", "stop-color": "#9a7942" })
    );
    defs.append(gradient);
    svg.append(defs);
    const pathData = "M30 187 C74 184 76 151 119 153 C163 155 164 122 207 123 C249 124 250 87 289 86 C316 85 322 52 344 28";
    svg.append(svgNode("path", { d: pathData, class: "crash-trajectory" }));
    const progressPath = svgNode("path", {
      d: pathData, class: "crash-trajectory-progress", id: "crashTrajectoryProgress",
      pathLength: "100", "stroke-dasharray": "100", "stroke-dashoffset": "100",
    });
    svg.append(progressPath);
    if (withPlane) {
      const plane = svgNode("g", { id: "crashPlane", class: "crash-plane-svg" });
      plane.append(
        svgNode("path", {
          d: "M-22-.8 Q-15-1.5-8-3 L1-5 6-14 Q7-16 9-14 L6-3 18-1.2 Q22 0 18 1.2L6 3 9 14Q7 16 6 14L1 5-8 3Q-15 1.5-22 .8Z",
          fill: "url(#crash-flying-plane-g)", stroke: "#fff0c7", "stroke-width": ".9",
        }),
        svgNode("path", { d: "M-17 0H16", fill: "none", stroke: "#fff8e7", "stroke-width": ".7", opacity: ".8" }),
        svgNode("path", { d: "M8-1.7Q12 0 8 1.7L4 1 5-1Z", fill: "#33414a", stroke: "#ddc99a", "stroke-width": ".55" }),
        svgNode("rect", { x: "-1.5", y: "-10.5", width: "4", height: "5", rx: "1.7", fill: "#6d695a", stroke: "#e8d5a5", "stroke-width": ".55" }),
        svgNode("rect", { x: "-1.5", y: "5.5", width: "4", height: "5", rx: "1.7", fill: "#6d695a", stroke: "#e8d5a5", "stroke-width": ".55" })
      );
      svg.append(plane);
    }
    return svg;
  }

  function updateCrashFlight() {
    const game = state.crash;
    const display = $("#crashMultiplier");
    if (!game || game.status !== "flying" || !display) return;
    const value = crashReadout();
    display.textContent = `×${value.toFixed(2)}`;
    const trajectory = $("#crashTrajectoryProgress");
    const plane = $("#crashPlane");
    const path = $(".crash-trajectory");
    if (trajectory && plane && path) {
      const progress = Math.min(0.995, Math.max(0, Math.log(Math.max(1, value)) / Math.log(50)));
      const length = path.getTotalLength();
      const distance = length * progress;
      const point = path.getPointAtLength(distance);
      const next = path.getPointAtLength(Math.min(length, distance + 1.5));
      const angle = Math.atan2(next.y - point.y, next.x - point.x) * 180 / Math.PI;
      plane.setAttribute("transform", `translate(${point.x} ${point.y}) rotate(${angle}) scale(.58)`);
      trajectory.style.strokeDashoffset = String(100 * (1 - progress));
    }
    const payout = $("#crashPotential");
    if (payout) payout.textContent = money(Math.floor(Number(game.stake) * value));
  }

  async function crashRequest(action) {
    if (state.crashBusy) return;
    state.crashBusy = true;
    renderCrash($("#games"));
    try {
      let data;
      if (action === "start") {
        data = await api("/api/crash/start", {
          method: "POST", body: { amount: Number(state.crashStake || 10) },
        });
      } else {
        data = await api("/api/crash/cashout", {
          method: "POST", body: { game_id: Number(state.crash && state.crash.id) },
        });
      }
      state.crash = data.game || null;
      if (data.balance !== undefined) setBalance(data.balance);
      if (action === "cashout") {
        if (state.crash && state.crash.status === "cashed") {
          toast(`Забрано ${money(state.crash.payout)} · ×${Number(state.crash.cashout_multiplier).toFixed(2)}`, "ok");
          haptic("ok");
        } else {
          toast(`Самолётик улетел на ×${Number(state.crash && state.crash.crash_multiplier || 1).toFixed(2)} · ставка потеряна`, "bad");
          haptic("bad");
        }
      } else {
        haptic("light");
      }
      renderCrash($("#games"));
      if (state.crash && state.crash.status !== "flying") refreshState({ silent: true });
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      state.crashBusy = false;
      renderCrash($("#games"));
    }
  }

  function renderCrash(host) {
    if (!host) return;
    if (state.crashTicker) { clearInterval(state.crashTicker); state.crashTicker = null; }
    host.textContent = "";
    host.append(gameBackButton());
    const game = state.crash;
    const flying = Boolean(game && game.status === "flying");
    const card = el("section", "crash-card" + (flying ? " in-flight" : ""));
    card.append(el("div", "crash-kicker", "КАССА · ИГРА НА ВЫЛЕТ"));
    card.append(el("h2", "crash-title", "Самолётик"));
    card.append(el("p", "crash-copy", "Множитель растёт, пока самолёт в полёте. Забери выплату до того, как он улетит."));

    const stage = el("div", "crash-stage" + (flying ? " live" : "") + (game && game.status === "crashed" ? " crashed" : ""));
    const sky = el("div", "crash-stars");
    stage.append(sky, crashFlightScene(flying));
    if (flying) {
      const multiplier = el("div", "crash-multiplier", `×${crashReadout().toFixed(2)}`);
      multiplier.id = "crashMultiplier";
      stage.append(multiplier);
      const timer = el("div", "crash-stage-caption", "ПОЛЁТ ИДЁТ · РЕШЕНИЕ ЗА ТОБОЙ");
      stage.append(timer);
    } else if (game && game.status === "crashed") {
      stage.append(el("div", "crash-settled-label", "УЛЕТЕЛ"));
      stage.append(el("div", "crash-result-x", `×${Number(game.crash_multiplier).toFixed(2)}`));
      stage.append(el("div", "crash-stage-caption", "СЛЕДУЮЩИЙ ВЗЛЁТ УЖЕ БЛИЗКО"));
    } else if (game && game.status === "cashed") {
      stage.append(el("div", "crash-settled-label safe", "ЗАБРАНО"));
      stage.append(el("div", "crash-result-x", `×${Number(game.cashout_multiplier).toFixed(2)}`));
      stage.append(el("div", "crash-stage-caption", `ВЫПЛАТА ${money(game.payout)}`));
    } else {
      stage.append(el("div", "crash-idle-mark", "✈"));
      stage.append(el("div", "crash-idle-title", "ГОТОВ К ВЗЛЁТУ"));
      stage.append(el("div", "crash-stage-caption", "ЗАБИРАЙ В СВОЙ МОМЕНТ"));
    }
    card.append(stage);

    if (flying) {
      const info = el("div", "crash-flight-meta");
      const left = el("div");
      left.append(el("span", null, "Твоя ставка"), el("b", null, money(game.stake)));
      const right = el("div");
      right.append(el("span", null, "Забрать сейчас"));
      const potential = el("b", null, money(Math.floor(Number(game.stake) * crashReadout())));
      potential.id = "crashPotential";
      right.append(potential);
      info.append(left, right);
      card.append(info);
      const cash = el("button", "btn crash-cashout", `ЗАБРАТЬ · ${money(Math.floor(Number(game.stake) * crashReadout()))}`);
      cash.dataset.crashAction = "cashout";
      cash.id = "crashCashout";
      cash.disabled = state.crashBusy;
      card.append(cash);
      if (state.crashTicker) clearInterval(state.crashTicker);
      state.crashTicker = window.setInterval(() => {
        if (!$("#crashMultiplier") || !state.crash || state.crash.status !== "flying") {
          clearInterval(state.crashTicker); state.crashTicker = null; return;
        }
        updateCrashFlight();
        const button = $("#crashCashout");
        const value = crashReadout();
        if (button && !state.crashBusy) button.textContent = `ЗАБРАТЬ · ${money(Math.floor(Number(state.crash.stake) * value))}`;
      }, 60);
      card.append(el("div", "crash-foot", "Нажатие фиксирует текущий множитель на сервере. Если самолёт улетит первым — ставка сгорит."));
    } else {
      if (game && game.status === "crashed") {
        card.append(el("div", "crash-message lost", `Полёт завершился на ×${Number(game.crash_multiplier).toFixed(2)}. Ставка ${money(game.stake)} потеряна.`));
      } else if (game && game.status === "cashed") {
        card.append(el("div", "crash-message won", `Выплата ${money(game.payout)} зачислена · чистый результат ${moneySigned(game.payout - game.stake)}.`));
      }
      const amount = Number(state.crashStake || 50);
      const stakes = el("div", "crash-stakes");
      stakes.append(el("span", null, "Ставка"));
      [10, 50, 100, 500].forEach((value) => {
        const chip = el("button", "crash-chip" + (value === amount ? " active" : ""), String(value));
        chip.type = "button";
        chip.dataset.crashStake = String(value);
        chip.disabled = state.crashBusy || balance() < value;
        stakes.append(chip);
      });
      card.append(stakes);
      const input = el("input", "crash-stake-input");
      input.type = "number";
      input.inputMode = "numeric";
      input.min = "10";
      input.max = "5000";
      input.value = String(amount);
      input.dataset.crashInput = "true";
      input.setAttribute("aria-label", "Ставка в самолётике");
      input.disabled = state.crashBusy;
      card.append(input);
      const start = el("button", "btn crash-start", `ВЗЛЁТ · ${money(amount)}`);
      start.dataset.crashAction = "start";
      start.disabled = state.crashBusy || amount < 10 || amount > 5000 || amount > balance();
      card.append(start);
      card.append(el("div", "crash-economy", "Целевой RTP 95,5 % · ставка 10–5 000 монет. Выплата включает ставку; округление монет вниз может немного снизить фактический RTP на малых ставках."));
      card.append(el("div", "crash-foot", "Каждый полёт независим. История не предсказывает следующий вылет."));
    }
    host.append(card);
  }

  window.setInterval(async () => {
    if (!state.user || state.tab !== "games" || state.game !== "crash" || !state.crash ||
        state.crash.status !== "flying" || state.crashBusy || state.crashPolling) return;
    state.crashPolling = true;
    const oldStatus = state.crash.status;
    try {
      const data = await api("/api/crash/state");
      state.crash = data.game || null;
      if (data.balance !== undefined && Number(data.balance) !== Number(state.user.balance)) setBalance(data.balance);
      if (state.crash && state.crash.status === "flying") updateCrashFlight();
      else if (oldStatus === "flying") {
        renderCrash($("#games"));
        if (state.crash && state.crash.status === "crashed") {
          toast(`Самолётик улетел на ×${Number(state.crash.crash_multiplier).toFixed(2)}`, "bad");
          haptic("bad");
        }
        refreshState({ silent: true });
      }
    } catch (err) {
      // Потеря сети не меняет исход: сервер хранит ставку и точку вылета.
    } finally {
      state.crashPolling = false;
    }
  }, 700);

  function ladderDoorCount(count) {
    const value = Math.abs(Number(count)) % 100;
    const last = value % 10;
    if (value >= 11 && value <= 14) return `${count} дверей`;
    if (last === 1) return `${count} дверь`;
    if (last >= 2 && last <= 4) return `${count} двери`;
    return `${count} дверей`;
  }

  function ladderMultiplier(roundNo) {
    let factorial = 1;
    for (let n = 2; n <= roundNo + 1; n += 1) factorial *= n;
    return 0.955 * factorial;
  }

  async function ladderRequest(action, payload) {
    state.ladderBusy = true;
    renderLadder($("#games"));
    try {
      const data = await api(`/api/ladder/${action}`, { method: "POST", body: payload });
      state.ladder = data.game;
      if (state.user && data.balance !== undefined) setBalance(data.balance);
      haptic(data.game.status === "lost" ? "bad" : "ok");
      renderLadder($("#games"));
      if (data.game.status === "lost") toast("Промах — ставка проиграна", "bad");
      else if (data.game.status === "complete") toast(`Полный проход! Выплата ${money(data.game.payout)}`, "ok");
      else if (data.game.status === "cashed") toast(`Выплата ${money(data.game.payout)} зачислена`, "ok");
      refreshState({ silent: true });
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      state.ladderBusy = false;
      renderLadder($("#games"));
    }
  }

  function renderLadder(host) {
    if (!host) return;
    host.textContent = "";
    host.append(gameBackButton());
    const game = state.ladder;
    const card = el("section", "ladder-card");
    card.append(el("div", "ladder-kicker", "РИСКНИ — ИЛИ ЗАБЕРИ СВОЁ"));
    card.append(el("h2", "ladder-title", "Лесенка"));
    card.append(el("p", "ladder-copy", "Найди приз за одной из закрытых дверей. С каждым этажом дверей становится больше."));

    const active = game && ["choosing", "won"].includes(game.status);
    if (active) {
      const round = Number(game.round);
      const windows = Number(game.windows);
      const payout = Number(game.payout || Math.round(Number(game.stake) * ladderMultiplier(round)));
      const progress = el("div", "ladder-progress");
      for (let n = 1; n <= 8; n += 1) {
        const step = el("span", n < round ? "done" : n === round ? "current" : "", String(n));
        progress.append(step);
      }
      card.append(progress);
      const bar = el("div", "ladder-floorhead");
      bar.append(el("b", null, `Этаж ${round} из 8`));
      bar.append(el("span", null, `${ladderDoorCount(windows)} · выплата ${money(payout)}`));
      card.append(bar);

      const doors = el("div", "ladder-doors");
      for (let i = 0; i < windows; i += 1) {
        const door = el("button", "ladder-door");
        door.type = "button";
        door.dataset.ladderWindow = String(i);
        door.disabled = game.status !== "choosing" || state.ladderBusy;
        door.append(el("span", "ladder-door-icon"), el("b", null, `Дверь ${i + 1}`));
        if (game.status === "won" && Number(game.picked_index) === i) {
          door.classList.add("prize");
          door.append(el("small", null, "ПРИЗ"));
        } else if (game.status === "lost" && Number(game.revealed_index) === i) {
          door.classList.add("prize");
          door.append(el("small", null, "ПРИЗ"));
        }
        if (game.status === "lost" && Number(game.picked_index) === i) {
          door.classList.add("miss");
          door.append(el("small", null, "ВАШ ВЫБОР"));
        }
        doors.append(door);
      }
      card.append(doors);

      if (game.status === "choosing") {
        card.append(el("div", "ladder-note", "Выбери дверь — приз спрятан за одной из них."));
      } else if (round < 8) {
        card.append(el("div", "ladder-success", "Нашёл приз! Забирай выплату или поднимайся выше."));
        const actions = el("div", "ladder-actions");
        const cash = el("button", "btn ladder-cash", `Забрать ${money(payout)}`);
        cash.type = "button";
        cash.dataset.ladderAction = "cashout";
        cash.disabled = state.ladderBusy;
        const next = el("button", "btn ladder-next", `Дальше · ${ladderDoorCount(windows + 1)}`);
        next.type = "button";
        next.dataset.ladderAction = "continue";
        next.disabled = state.ladderBusy;
        actions.append(cash, next);
        card.append(actions);
      }
    } else {
      if (game && game.status === "lost") {
        card.append(el("div", "ladder-result lost", `Не повезло на этаже ${game.round}. Ставка ${money(game.stake)} потеряна.`));
        const doors = el("div", "ladder-doors final");
        for (let i = 0; i < Number(game.windows); i += 1) {
          const door = el("div", "ladder-door revealed" + (i === Number(game.revealed_index) ? " prize" : "") + (i === Number(game.picked_index) ? " miss" : ""));
          door.append(el("span", "ladder-door-icon"), el("b", null, `Дверь ${i + 1}`));
          if (i === Number(game.revealed_index)) door.append(el("small", null, "ПРИЗ"));
          doors.append(door);
        }
        card.append(doors);
      } else if (game && ["cashed", "complete"].includes(game.status)) {
        card.append(el("div", "ladder-result won", game.status === "complete" ? `Восемь этажей пройдены! Выплата ${money(game.payout)} зачислена.` : `Выплата ${money(game.payout)} зачислена после ${game.round} этажа.`));
      }
      const limits = state.limits || {};
      const maxStake = Number(limits.ladder_max_bet || 50);
      const minStake = 10;
      const picker = el("div", "ladder-stakes");
      picker.append(el("span", null, "Ставка"));
      [10, 25, maxStake].filter((value, index, list) => value >= minStake && list.indexOf(value) === index).forEach((value) => {
        const chip = el("button", "ladder-stake" + (Number(state.ladderStake) === value ? " active" : ""), money(value));
        chip.type = "button";
        chip.dataset.ladderStake = String(value);
        chip.disabled = state.ladderBusy || balance() < value;
        picker.append(chip);
      });
      card.append(picker);
      const start = el("button", "btn ladder-start", game && game.status === "lost" ? "Ещё раз" : "Начать · 10 🪙");
      start.type = "button";
      start.dataset.ladderAction = "start";
      start.disabled = state.ladderBusy || balance() < Number(state.ladderStake || minStake);
      start.textContent = `Начать · ${money(Number(state.ladderStake || minStake))}`;
      card.append(start);
      card.append(el("div", "ladder-note", `RTP 95,5 % · ставка ${minStake}–${maxStake} 🪙 · максимальная выплата при ставке ${maxStake} — ${money(Math.round(maxStake * ladderMultiplier(8)))}`));
    }
    card.append(el("div", "ladder-foot", "Выплата включает ставку. Ошибка на любом этаже завершает игру без выплаты."));
    host.append(card);
  }

  function blackjackCardNode(card) {
    const node = el("div", "bj-card" + (card.hidden ? " hidden" : "") +
      (card.suit === "♥" || card.suit === "♦" ? " red" : ""));
    if (card.hidden) {
      node.setAttribute("aria-label", "Закрытая карта");
      node.append(el("span", "bj-back", "✦"));
      return node;
    }
    node.setAttribute("aria-label", `${card.rank} ${card.suit}`);
    node.append(el("b", "bj-rank", card.rank), el("span", "bj-suit", card.suit));
    return node;
  }

  function blackjackStakeBlock() {
    const { min, max } = casinoLimits();
    const available = Math.max(min, Math.min(max, balance()));
    const initial = clampStake(state.blackjackStake || min, min, available);
    state.blackjackStake = initial;
    const block = el("div", "stake-block bj-stake-block");
    block.append(el("div", "bj-stake-label", "Ставка на раздачу"));
    const presets = el("div", "stake-presets");
    const values = pickPresets(min, available, 4);
    if (available > values[values.length - 1]) values.push(available);
    values.forEach((value) => {
      const chip = el("button", "stake-chip" + (value === initial ? " active" : ""),
        value === available && value > values[0] && value > min * 50 ? "Всё" : money(value));
      chip.type = "button";
      chip.dataset.blackjackStake = String(value);
      presets.append(chip);
    });
    presets.style.gridTemplateColumns = `repeat(${values.length}, minmax(0, 1fr))`;

    const input = el("input", "stake-input");
    input.id = "blackjackStakeInput";
    input.type = "number";
    input.inputMode = "numeric";
    input.min = String(min);
    input.max = String(max);
    input.value = String(initial);
    input.setAttribute("aria-label", "Сумма ставки в блэкджеке");
    const start = el("button", "btn play bj-start", "");
    start.type = "button";
    const update = () => {
      const amount = Math.round(Number(input.value) || 0);
      state.blackjackStake = amount;
      presets.querySelectorAll(".stake-chip").forEach((chip) => {
        chip.classList.toggle("active", Number(chip.dataset.blackjackStake) === amount);
      });
      const valid = amount >= min && amount <= max && amount <= balance();
      start.disabled = state.blackjackBusy || !valid;
      start.textContent = amount > balance() ? "Не хватает монет" : `Раздать карты · ${money(amount || min)}`;
    };
    values.forEach((value) => {
      const chip = presets.querySelector(`[data-blackjack-stake="${value}"]`);
      chip.addEventListener("click", () => { input.value = String(value); update(); });
    });
    input.addEventListener("focus", () => { state.blackjackTyping = true; });
    input.addEventListener("input", update);
    input.addEventListener("blur", () => { state.blackjackTyping = false; });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    });
    start.addEventListener("click", () => startBlackjack(Number(input.value)));
    block.append(presets, input, start);
    update();
    return block;
  }

  function renderBlackjack(host) {
    host.textContent = "";
    const game = state.blackjack;
    const table = el("div", "blackjack-table");
    const heading = el("div", "bj-heading");
    heading.append(el("div", "bj-kicker", "КЛАССИЧЕСКАЯ ИГРА 21"));
    heading.append(el("h2", null, "Блэкджек"));
    heading.append(el("div", "bj-rules-line", "Натуральный blackjack — 3:2 · дилер стоит на 17"));
    table.append(heading);

    const showCards = game && (game.status === "insurance" || game.status === "playing" || game.status === "settled");
    if (showCards) {
      const dealer = el("section", "bj-dealer");
      const dealerHead = el("div", "bj-seat-head");
      dealerHead.append(el("b", null, "Дилер"));
      dealerHead.append(el("span", null, game.dealer.hidden ? "закрытая карта" : `${game.dealer.total}${game.dealer.soft ? " · мягкая" : ""}`));
      dealer.append(dealerHead);
      const dealerCards = el("div", "bj-cards");
      game.dealer.cards.forEach((card) => dealerCards.append(blackjackCardNode(card)));
      dealer.append(dealerCards);
      table.append(dealer);

      const players = el("section", "bj-player-hands");
      game.hands.forEach((hand, index) => {
        const card = el("div", "bj-hand" + (hand.active ? " active" : "") + (hand.busted ? " busted" : ""));
        const handHead = el("div", "bj-seat-head");
        const label = game.hands.length > 1 ? `Рука ${index + 1}` : "Твои карты";
        handHead.append(el("b", null, `${label} · ${money(hand.bet)}`));
        const status = hand.active ? "твой ход" : hand.result || (hand.stood ? "остановился" : hand.busted ? "перебор" : "");
        handHead.append(el("span", null, status));
        card.append(handHead);
        const cards = el("div", "bj-cards");
        hand.cards.forEach((item) => cards.append(blackjackCardNode(item)));
        card.append(cards);
        const total = el("div", "bj-total", `${hand.total}${hand.soft && hand.total <= 21 ? " · мягкая рука" : ""}`);
        card.append(total);
        players.append(card);
      });
      table.append(players);
    }

    if (game && game.status === "insurance") {
      const insurance = el("div", "bj-insurance");
      insurance.append(el("b", null, "Дилер показывает туза"));
      insurance.append(el("span", null,
        `Страховка ${money(game.insurance.amount)} · при блэкджеке вернётся ${money(game.insurance.amount * 3)}.`));
      const choices = el("div", "bj-actions");
      const take = el("button", "btn", `Застраховаться · ${money(game.insurance.amount)}`);
      take.type = "button";
      take.disabled = state.blackjackBusy || !game.insurance.can_take;
      take.addEventListener("click", () => blackjackAction("insurance"));
      const decline = el("button", "btn ghost", "Играть без страховки");
      decline.type = "button";
      decline.disabled = state.blackjackBusy;
      decline.addEventListener("click", () => blackjackAction("decline_insurance"));
      choices.append(take, decline);
      insurance.append(choices);
      if (!game.insurance.can_take) insurance.append(el("small", null, "Не хватает монет на страховку — можно продолжить без неё."));
      table.append(insurance);
    } else if (game && game.status === "playing") {
      const hand = game.hands[game.active_hand];
      const actions = el("div", "bj-actions");
      [
        ["hit", "Взять карту", hand && hand.can_hit],
        ["stand", "Остановиться", hand && hand.can_stand],
        ["double", `Удвоить · ${money(hand ? hand.bet : 0)}`, hand && hand.can_double],
        ["split", `Разделить · ${money(hand ? hand.bet : 0)}`, hand && hand.can_split],
      ].forEach(([action, label, enabled]) => {
        const button = el("button", "btn" + (action === "hit" ? " play" : " ghost"), label);
        button.type = "button";
        button.disabled = state.blackjackBusy || !enabled;
        button.addEventListener("click", () => blackjackAction(action));
        actions.append(button);
      });
      table.append(actions);
      if (hand && hand.split_aces) table.append(el("div", "bj-note", "После разделения тузов раздаётся по одной карте, затем руки останавливаются."));
    }

    if (game && game.status === "settled" && game.result) {
      const result = el("div", "bj-result " + (game.result.net > 0 ? "positive" : game.result.net < 0 ? "negative" : "even"));
      result.append(el("b", null, game.result.label));
      const netText = `${game.result.net > 0 ? "+" : game.result.net < 0 ? "−" : ""}${money(Math.abs(game.result.net))}`;
      result.append(el("span", null, `Выплата ${money(game.result.payout)} · чистая прибыль ${netText}`));
      if (game.insurance.taken) {
        result.append(el("small", null, `Страховка ${money(game.insurance.taken)} · выплата ${money(game.insurance.payout)}`));
      }
      table.append(result);
    }

    if (!game || game.status === "settled") {
      const startBox = el("div", "bj-start-box");
      startBox.append(el("p", null, game ? "Раздача завершена — сыграем ещё?" : "Выбери ставку и начни раздачу."));
      startBox.append(blackjackStakeBlock());
      table.append(startBox);
    }
    const footer = el("div", "bj-footnote",
      "Обычная победа приносит чистую прибыль 1:1, ничья возвращает ставку; натуральный блэкджек — 3:2. " +
      "Дилер стоит на любой 17. Удвоение доступно на первых двух картах; сплит одинаковых рангов — до 4 рук. " +
      "Тузы нельзя делить повторно, после сплита тузов выдаётся по одной карте. Используется одна колода из 52 карт: " +
      "одинаковые карты не повторяются. Страховка против туза стоит половину ставки, при блэкджеке дилера возвращает 3× сумму страховки.");
    table.append(footer);
    host.append(table, summaryNode(), historyNode());
  }

  async function startBlackjack(amount) {
    if (state.blackjackBusy) return;
    const { min, max } = casinoLimits();
    if (amount < min || amount > max) { toast(`Ставка — от ${money(min)} до ${money(max)}`, "bad"); return; }
    if (amount > balance()) { toast("Недостаточно монет на балансе", "bad"); return; }
    state.blackjackBusy = true;
    renderGames();
    try {
      const data = await api("/api/blackjack/start", {
        method: "POST", body: { amount, token: newToken() },
      });
      state.blackjack = data.game;
      setBalance(data.balance);
      haptic("light");
      await refreshState({ silent: true });
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      state.blackjackBusy = false;
      renderGames();
    }
  }

  async function blackjackAction(action) {
    if (state.blackjackBusy || !state.blackjack) return;
    const gameId = state.blackjack.id;
    state.blackjackBusy = true;
    renderGames();
    try {
      const data = await api("/api/blackjack/action", {
        method: "POST", body: {
          game_id: gameId, action, revision: state.blackjack.revision, token: newToken(),
        },
      });
      state.blackjack = data.game;
      setBalance(data.balance);
      haptic(data.game.status === "settled" ? "ok" : "light");
      if (data.game.status === "settled") await refreshState({ silent: true });
    } catch (err) {
      toast(err.message, "bad");
      await refreshState({ silent: true });
    } finally {
      state.blackjackBusy = false;
      renderGames();
    }
  }

  function gameBackButton() {
    const button = el("button", "btn ghost game-back", "‹ Все игры");
    button.type = "button";
    button.dataset.backToGames = "true";
    return button;
  }

  function renderGames() {
    const host = $("#games");
    if (!host) return;
    const menu = $("#gameSwitch");
    const screen = $("#screen-games");
    if (screen) screen.classList.toggle("catalog-mode", !state.game);

    document.querySelectorAll("#gameSwitch .game-tile").forEach((btn) => {
      const active = Boolean(state.game) && btn.dataset.game === state.game;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
    if (menu) menu.hidden = Boolean(state.game);

    if (!state.game) {
      host.textContent = "";
      return;
    }
    if (!state.user) {
      host.textContent = "";
      host.append(gameBackButton(), el("div", "empty", "Открой приложение кнопкой в боте — тогда появятся игры."));
      return;
    }
    if (state.game === "ladder") {
      renderLadder(host);
      return;
    }
    if (state.game === "blackjack") {
      renderBlackjack(host);
      host.prepend(gameBackButton());
      return;
    }
    if (state.game === "crash") {
      renderCrash(host);
      return;
    }
    if (!state.casino) {
      host.textContent = "";
      host.append(gameBackButton(), el("div", "skeleton"), el("div", "skeleton"));
      return;
    }

    host.textContent = "";
    host.append(gameBackButton());
    const game = state.game;
    const pick = pickFor(game);

    host.append(tableHead(game));
    host.append(outcomeNode());

    const field = el("div", "casino-field");
    if (game === "roulette") {
      const numbers = el("div", "bet-group");
      numbers.append(el("h4", null, "Точное число"));
      numbers.append(numberBoard(pick));
      field.append(numbers);
    }
    optionGroups(game).forEach((group) => {
      const block = el("div", "bet-group");
      block.append(el("h4", null, group.name));
      const grid = el("div", "bet-grid");
      // длинные подписи («Меньше 7 (2–6)», «Ровно 12») не влезают в строку с кэфом
      if (group.items.some((option) => String(option.label).length > 7)) grid.classList.add("stack");
      group.items.forEach((option) => grid.append(betChip(option, option.code === pick)));
      block.append(grid);
      field.append(block);
    });
    host.append(field);

    host.append(stakeRow(game));

    const info = potential(game);
    const meta = el("div", "bet-meta");
    meta.id = "betMeta";
    meta.append(el("span", null, "Ставка: " + money(info.stake)));
    meta.append(el("span", null,
      "кэф ×" + (info.odds ? info.odds.toFixed(2) : "—") + " · выплата " +
      (info.payout === null ? "—" : money(info.payout)) + " · чистая прибыль " +
      (info.profit === null ? "—" : moneySigned(info.profit))));
    host.append(meta);

    const play = el("button", "btn play");
    play.id = "playBtn";
    play.type = "button";
    play.textContent = (game === "roulette" ? "Крутить колесо" : "Бросить кости") + " · " + money(info.stake);
    host.append(play);

    host.append(summaryNode());
    host.append(historyNode());
    host.append(rulesNode(game));

    paintOutcome(state.casinoResults[game]);
    // перерисовали экран посреди раунда — блокировка должна остаться
    if (state.casinoBusy) lockTable(true);
  }

  // ------------------------------------------------------- анимация рулетки
  function spinRouletteTo(number) {
    const svg = $("#rouletteSvg");
    const ball = $("#rouletteBall");
    const hub = $("#rouletteHub");
    const index = ROULETTE_ORDER.indexOf(Number(number));
    const step = ROULETTE_STEP;

    return new Promise((resolve) => {
      if (!svg) { setTimeout(resolve, 500); return; }

      // доворачиваем так, чтобы нужная лунка встала под указатель (12 часов);
      // jitter — колесо почти никогда не останавливается ровно по центру лунки
      const centre = index * step + step / 2;
      const jitter = (Math.random() - 0.5) * step * 0.5;
      const current = state.rouletteAngle || 0;
      const target = current + 6 * 360 + (((-centre - jitter - current) % 360) + 360) % 360;

      svg.classList.add("spinning");
      void svg.getBoundingClientRect();     // иначе браузер склеит два transform
      svg.style.transform = `rotate(${target}deg)`;
      state.rouletteAngle = target;

      if (hub) {
        hub.className = "roulette-hub";
        hub.textContent = "";
        hub.append(el("b", null, "…"), el("small", null, "крутим"));
      }

      if (ball) {
        // шарик идёт против колеса и замирает ровно наверху — в выигрышной лунке
        const start = state.ballAngle || 0;
        const laps = Math.ceil((5 * 360 + start) / 360);
        const ballTarget = -360 * laps + (Math.random() - 0.5) * 3;
        ball.classList.add("spinning");
        void ball.getBoundingClientRect();
        ball.style.transform = `rotate(${ballTarget}deg)`;
        state.ballAngle = ballTarget;
      }

      setTimeout(() => {
        svg.classList.remove("spinning");
        if (ball) ball.classList.remove("spinning");
        if (hub) {
          const colour = rouletteColour(number);
          hub.className = "roulette-hub settled " + colour;
          hub.textContent = "";
          hub.append(
            el("b", null, String(number)),
            el("small", null, colour === "green" ? "зеро" : colour === "red" ? "красное" : "чёрное"),
          );
        }
        resolve();
      }, 4980);
    });
  }

  // --------------------------------------------------------- анимация костей
  function rollDiceTo(values) {
    const dice = [$("#dieA"), $("#dieB")];
    if (!dice[0] || !dice[1]) return new Promise((resolve) => setTimeout(resolve, 700));

    dice.forEach((die, index) => {
      const cube = die.querySelector(".die-cube");
      const base = DIE_ORIENT[values[index]] || { x: 0, y: 0 };
      const was = state.diceAngles[index] || { x: -18, y: 22 };
      // всегда вперёд: минимум три оборота по каждой оси, потом нужная грань вверх
      const xTurns = Math.ceil((was.x + 1080 - base.x) / 360);
      const yTurns = Math.ceil((was.y + 1080 + index * 180 - base.y) / 360);
      const angles = { x: base.x + 360 * xTurns, y: base.y + 360 * yTurns };
      state.diceAngles[index] = angles;
      cube.style.transform = `rotateX(${angles.x}deg) rotateY(${angles.y}deg)`;
      die.classList.remove("lifted");
      void die.getBoundingClientRect();
      die.classList.add("lifted");
    });

    state.diceShown = values.slice();
    const total = $("#diceSum");
    if (total) total.textContent = "бросаем…";

    return new Promise((resolve) => {
      setTimeout(() => {
        dice.forEach((die) => die.classList.remove("lifted"));
        if (total) total.textContent = `сумма ${values[0] + values[1]}`;
        resolve();
      }, 1320);
    });
  }

  // ------------------------------------------------------------- сам раунд
  function lockTable(busy) {
    const play = $("#playBtn");
    if (play) {
      play.disabled = busy;
      if (busy) play.textContent = state.game === "roulette" ? "Крутим…" : "Бросаем…";
    }
    document.querySelectorAll("#games .bet-chip, #games .numboard button, #games .stake-chip, #games .stake-step, #stakeInput")
      .forEach((node) => { node.disabled = busy; });
  }

  function refreshSide() {
    const summary = $("#casinoSummary");
    if (summary) summary.replaceWith(summaryNode());
    const hist = $("#casinoHist");
    if (hist) hist.replaceWith(historyNode());
    syncStakeUI(state.game);
  }

  async function playCasinoRound() {
    if (state.casinoBusy || !state.casino || !state.user) return;
    const game = state.game;
    const selection = pickFor(game);
    const amount = Math.round(Number(state.casinoStake[game]) || 0);
    const info = stakeState(game);
    if (info.low) { toast(`Минимум — ${money(info.min)}`, "bad"); haptic("bad"); return; }
    if (info.high) { toast(`Максимум за раунд — ${money(info.max)}`, "bad"); haptic("bad"); return; }
    if (!canBet() || amount > balance()) {
      haptic("bad");
      openNoCoinsSheet(minBet());
      return;
    }

    state.casinoBusy = true;
    state.casinoToken = state.casinoToken || newToken();
    const token = state.casinoToken;
    state.casinoResults[game] = null;
    paintOutcome(null);
    lockTable(true);

    try {
      const data = await api("/api/casino/play", {
        method: "POST",
        body: { game, selection, amount, token },
      });
      state.casinoToken = null;   // раунд состоялся: следующий — с новой меткой
      // экран мог уехать на другую вкладку или в другую игру, пока крутилось колесо

      if (state.game === game) {
        if (game === "roulette") await spinRouletteTo(data.number);
        else await rollDiceTo(data.dice);
      }

      setBalance(data.balance);
      state.casinoResults[game] = data;
      const onScreen = state.tab === "games" && state.game === game;
      if (onScreen) paintOutcome(data, { pop: true });
      else {
        // результат некуда рисовать — говорим тостом, но именно про эту игру
        const name = game === "roulette" ? "Рулетка" : "Кости";
        toast(`${name} · ${outcomeText(data)} · ` +
          (data.win
            ? `выплата ${money(data.payout)} · чистая прибыль ${moneySigned(data.payout - amount)}`
            : `проигрыш ${money(amount)}`), data.win ? "ok" : "bad");
      }
      haptic(data.win ? "ok" : "bad");

      if (game === "roulette" && state.game === game) {
        pushNumber(data.number);
        const strip = $("#numStrip");
        if (strip && strip.children.length) strip.children[0].classList.remove("fresh");
      } else if (game === "roulette") {
        state.recentNumbers = [data.number].concat(state.recentNumbers || []).slice(0, 10);
      }

      // сводку и историю ведём локально, чтобы не перерисовывать колесо
      const summary = state.casino.summary;
      summary.rounds += 1;
      summary.staked += amount;
      summary.paid += data.payout;
      summary.net += data.payout - amount;
      if (data.win) summary.wins += 1;
      state.casino.history = [{
        id: data.round,
        game,
        label: data.label,
        amount,
        odds: data.odds,
        outcome: String(data.outcome),
        win: Boolean(data.win),
        payout: data.payout,
        at: Math.floor(Date.now() / 1000),
      }].concat(state.casino.history || []).slice(0, 12);

      if (data.win) {
        toast(`Выплата ${money(data.payout)} · чистая прибыль ${moneySigned(data.payout - amount)}`, "ok");
      }
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      state.casinoBusy = false;
      lockTable(false);
      refreshSide();
    }
  }

  function bindGames() {
    document.querySelectorAll("#gameSwitch .game-tile").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (state.game === btn.dataset.game) return;
        if (state.casinoBusy || state.ladderBusy || state.blackjackBusy || state.crashBusy) {
          // посреди раунда игру не меняем: иначе результат одной игры
          // оказался бы на экране другой
          haptic("bad");
          toast("Дождись конца раунда", "bad");
          return;
        }
        state.game = btn.dataset.game;
        haptic("light");
        renderGames();
      });
    });

    const host = $("#games");
    if (!host) return;

    host.addEventListener("click", (event) => {
      const node = event.target.closest("button");
      if (!node || node.disabled) return;
      const game = state.game;

      if (node.dataset.backToGames !== undefined) {
        if (state.casinoBusy || state.ladderBusy || state.blackjackBusy || state.crashBusy) {
          toast("Дождись конца хода", "bad");
          return;
        }
        state.game = null;
        haptic("light");
        renderGames();
        return;
      }

      if (node.dataset.crashAction) {
        crashRequest(node.dataset.crashAction);
        return;
      }
      if (node.dataset.crashStake) {
        state.crashStake = Number(node.dataset.crashStake);
        renderCrash(host);
        return;
      }
      if (node.dataset.ladderStake) {
        state.ladderStake = Number(node.dataset.ladderStake);
        renderLadder(host);
        return;
      }
      if (node.dataset.ladderWindow !== undefined && state.ladder) {
        ladderRequest("pick", { game_id: state.ladder.id, window_index: Number(node.dataset.ladderWindow) });
        return;
      }
      if (node.dataset.ladderAction) {
        if (node.dataset.ladderAction === "start") ladderRequest("start", { amount: Number(state.ladderStake || 10) });
        else if (node.dataset.ladderAction === "continue" && state.ladder) ladderRequest("continue", { game_id: state.ladder.id });
        else if (node.dataset.ladderAction === "cashout" && state.ladder) ladderRequest("cashout", { game_id: state.ladder.id });
        return;
      }

      if (node.id === "playBtn") { playCasinoRound(); return; }

      if (node.dataset.code) {
        state.casinoPick[game] = node.dataset.code;
        haptic("light");
        renderGames();
        return;
      }
      if (node.dataset.stake) {
        state.casinoStake[game] = Number(node.dataset.stake);
        haptic("light");
        syncStakeUI(game);
        return;
      }
      if (node.dataset.step) {
        const info = stakeState(game);
        const up = node.dataset.step === "1";
        const base = info.raw || info.min;
        let next = Math.round(up ? base * 1.5 : base / 1.5);
        if (up) next = Math.round(next / 10) * 10 || base + 10;
        state.casinoStake[game] = Math.min(info.max, Math.max(info.min, next));
        haptic("light");
        syncStakeUI(game);
      }
    });
    host.addEventListener("input", (event) => {
      const input = event.target.closest("[data-crash-input]");
      if (!input) return;
      const parsed = Number.parseInt(input.value, 10);
      state.crashStake = Number.isFinite(parsed) ? parsed : 0;
      const start = $(".crash-start");
      if (start) {
        start.textContent = `ВЗЛЁТ · ${money(Math.max(0, state.crashStake))}`;
        start.disabled = state.crashBusy || state.crashStake < 10 || state.crashStake > 5000 || state.crashStake > balance();
      }
      document.querySelectorAll(".crash-chip").forEach((chip) => {
        chip.classList.toggle("active", Number(chip.dataset.crashStake) === state.crashStake);
      });
    });
  }

  // ------------------------------------------------------------------- init
  let started = false;
  function init() {
    // скрипт подключён с defer: DOM уже готов, а DOMContentLoaded ещё впереди.
    // Без этой проверки init() выполнялся дважды и каждый обработчик навешивался
    // по два раза — двойной тап по фильтру возвращал состояние назад.
    if (started) return;
    started = true;
    renderHeader();
    renderMatches();
    renderExpress();
    renderCartBar();
    renderMe();
    switchTab("matches");

    document.querySelectorAll("nav.tabs button").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    const tourBtn = $("#tourFilter");
    if (tourBtn) {
      tourBtn.addEventListener("click", () => {
        const source = state.segment === "live"
          ? state.liveMatches
          : marketSource();
        openTournamentSheet(source
          .filter((m) => (m.sport || "cs2") === state.sport)
          .filter(inCategory));
      });
    }
    const tourReset = $("#tourReset");
    if (tourReset) {
      tourReset.addEventListener("click", () => { setTournament(null); renderMatches(); });
    }
    document.querySelectorAll("#sportSegments .seg").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (state.sport === btn.dataset.sport) return;
        state.sport = btn.dataset.sport;
        haptic("light");
        renderMatches();
        refreshState({ silent: true });
      });
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
    const favChip = $("#favFilter");
    if (favChip) {
      once(favChip, () => {
        state.onlyFavorites = !state.onlyFavorites;
        haptic("light");
        updateFavFilter();
        renderMatches();
        if (state.onlyFavorites && !state.favorites.length) {
          toast("Отметь команды звёздочкой — и они будут здесь");
        }
      });
      updateFavFilter();
    }
    bindGames();
    $("#cartOpen").addEventListener("click", () => switchTab("express"));
    const bell = $("#bonusBell");
    if (bell) bell.addEventListener("click", () => { haptic(); switchTab("bonus"); });
    $("#sheetWrap").addEventListener("click", (event) => {
      if (event.target === $("#sheetWrap")) closeSheet();
    });

    initializeTelegram();

    attachReferral();

    refreshState();
    loadBets();   // заодно ставит бейдж с числом ставок в игре

    // автообновление: кэфы и статусы матчей подтягиваются без перезахода.
    // В live-разделе линия ходит быстро, поэтому там опрашиваем чаще
    const pollTick = () => {
      const live = state.segment === "live" || state.segment === "all";
      setTimeout(async () => {
        if (!document.hidden) await refreshState({ silent: true });
        pollTick();
      }, live ? Math.round(POLL_MS / 2.5) : POLL_MS);
    };
    pollTick();

    // вернулись в приложение — сразу свежая линия
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshState({ silent: true });
    });
  }

  // Ссылка-приглашение: мини-апп открыли с параметром ref_<id>
  function attachReferral() {
    let param = "";
    try {
      param = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || "";
    } catch (e) { param = ""; }
    if (!param) {
      // в браузере параметр можно передать руками: ?ref_123
      const match = /ref_\d+/.exec(location.search + location.hash);
      if (match) param = match[0];
    }
    if (!/^ref_\d+$/.test(param)) return;
    if (localStorage.getItem("cs2bets_ref") === param) return;
    api("/api/referral", { method: "POST", body: { code: param } })
      .then(() => {
        try { localStorage.setItem("cs2bets_ref", param); } catch (e) { /* приватный режим */ }
      })
      .catch(() => { /* уже привязан или поздно — молчим */ });
  }

  document.addEventListener("DOMContentLoaded", init);
  if (document.readyState !== "loading") init();
})();
