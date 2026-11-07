/* ============================================================
   Мини-приложение: калькулятор + погода
   ============================================================ */

// ---------- 1. Telegram WebApp ----------
// Если страница открыта в обычном браузере (для отладки), Telegram будет undefined —
// поэтому везде используем безопасную проверку.
const tg = window.Telegram ? window.Telegram.WebApp : null;

if (tg) {
  tg.ready();          // сообщаем Telegram: "я загрузился"
  tg.expand();         // раскрываем на весь экран

  // Подстраиваемся под светлую/тёмную тему Telegram
  if (tg.colorScheme === "light") {
    document.body.classList.add("light");
  }

  // Здороваемся по имени пользователя
  const user = tg.initDataUnsafe && tg.initDataUnsafe.user;
  if (user && user.first_name) {
    document.getElementById("hello").textContent = `Привет, ${user.first_name}! 👋`;
  }
}

function haptic() {
  if (tg && tg.HapticFeedback) {
    tg.HapticFeedback.impactOccurred("light");
  }
}

// ---------- 2. Переключение вкладок ----------
const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((t) => t.classList.remove("active"));
    panels.forEach((p) => p.classList.remove("active"));

    tab.classList.add("active");
    document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
  });
});

/* ============================================================
   3. КАЛЬКУЛЯТОР
   ============================================================ */

const expressionEl = document.getElementById("calc-expression");
const resultEl = document.getElementById("calc-result");

let expression = "";     // то, что набрал пользователь (в "программном" виде)
let justEvaluated = false;

// Красивое отображение: * → ×, / → ÷, - → −
function pretty(text) {
  return text
    .replace(/\*/g, "×")
    .replace(/\//g, "÷")
    .replace(/-/g, "−");
}

function render(preview) {
  expressionEl.textContent = pretty(expression) || "\u00A0";

  // Живой предпросмотр результата, пока пользователь набирает
  if (preview && expression) {
    const value = safeCalculate(expression);
    resultEl.textContent = value === null ? "—" : value;
  }
}

function safeCalculate(raw) {
  if (!raw) return null;

  // Оставляем только цифры и разрешённые знаки — защита от мусора
  if (!/^[0-9+\-*/().\s]+$/.test(raw)) return null;

  let cleaned = raw.trim();
  // Убираем висящий знак в конце: "2+" → "2"
  cleaned = cleaned.replace(/[+\-*/.\s]+$/, "");
  if (!cleaned) return null;

  try {
    // Function вместо eval: код не имеет доступа к внешним переменным
    const value = Function('"use strict"; return (' + cleaned + ");")();
    if (typeof value !== "number" || !isFinite(value)) return null;
    // Округляем, чтобы не было 0.30000000000000004
    return Math.round(value * 1e10) / 1e10;
  } catch (e) {
    return null;
  }
}

function pressKey(key) {
  haptic();

  // На кнопках красивые × ÷ −, а внутри выражения считаем обычными * / -
  if (key === "×") key = "*";
  else if (key === "÷") key = "/";
  else if (key === "−") key = "-";

  switch (key) {
    case "C":
      expression = "";
      justEvaluated = false;
      resultEl.textContent = "0";
      render(false);
      return;

    case "back":
      expression = expression.slice(0, -1);
      justEvaluated = false;
      resultEl.textContent = "0";
      render(true);
      return;

    case "=": {
      const value = safeCalculate(expression);
      if (value === null) {
        resultEl.textContent = "Ошибка";
        return;
      }
      expression = String(value);
      justEvaluated = true;
      resultEl.textContent = value;
      expressionEl.textContent = pretty(String(value));
      return;
    }
  }

  const isOperator = "+-*/".includes(key);
  const lastChar = expression.slice(-1);
  const hasLast = expression.length > 0;

  // После "=" цифра начинает новое выражение, а оператор — продолжает
  if (justEvaluated) {
    justEvaluated = false;
    if (!isOperator) {
      expression = "";
    }
  }

  // Защита от "2++" и подобного
  if (isOperator) {
    if (!hasLast) {
      if (key === "-") expression += key;   // минус в начале
      render(true);
      return;
    }
    if ("+-*/".includes(expression.slice(-1))) {
      expression = expression.slice(0, -1) + key;
    } else if (lastChar === "(" && key !== "-") {
      // после "(" оператор не нужен
    } else {
      expression += key;
    }
    render(true);
    return;
  }

  // Умное умножение: "2(" → "2×("
  if (key === "(") {
    if (hasLast && /[0-9)]/.test(expression.slice(-1))) {
      expression += "*";
    }
    expression += "(";
    render(true);
    return;
  }

  if (key === ")") {
    const opens = (expression.match(/\(/g) || []).length;
    const closes = (expression.match(/\)/g) || []).length;
    if (opens > closes && !/[+\-*/(]$/.test(expression)) {
      expression += ")";
      render(true);
    }
    return;
  }

  if (key === ".") {
    // Не даём поставить две точки в одном числе
    const lastNumber = expression.split(/[^0-9.]/).pop();
    if (lastNumber.includes(".")) return;
    if (!lastNumber) {
      expression += "0.";
    } else {
      expression += ".";
    }
    render(true);
    return;
  }

  if (/[0-9]/.test(key)) {
    expression += key;
    render(true);
  }
}

document.querySelectorAll(".key").forEach((button) => {
  button.addEventListener("click", () => pressKey(button.dataset.key));
});

/* ============================================================
   4. ПОГОДА (Open-Meteo — без API-ключа)
   ============================================================ */

const cityInput = document.getElementById("city-input");
const weatherBtn = document.getElementById("weather-btn");
const statusEl = document.getElementById("weather-status");
const cardEl = document.getElementById("weather-card");

// Расшифровка кодов погоды Open-Meteo (WMO)
const WEATHER_CODES = {
  0:  { emoji: "☀️", text: "Ясно" },
  1:  { emoji: "🌤️", text: "Почти ясно" },
  2:  { emoji: "⛅", text: "Переменная облачность" },
  3:  { emoji: "☁️", text: "Пасмурно" },
  45: { emoji: "🌫️", text: "Туман" },
  48: { emoji: "🌫️", text: "Изморозь" },
  51: { emoji: "🌦️", text: "Слабая морось" },
  53: { emoji: "🌦️", text: "Морось" },
  55: { emoji: "🌧️", text: "Сильная морось" },
  61: { emoji: "🌧️", text: "Небольшой дождь" },
  63: { emoji: "🌧️", text: "Дождь" },
  65: { emoji: "🌧️", text: "Сильный дождь" },
  66: { emoji: "🌧️", text: "Ледяной дождь" },
  67: { emoji: "🌧️", text: "Сильный ледяной дождь" },
  71: { emoji: "🌨️", text: "Небольшой снег" },
  73: { emoji: "🌨️", text: "Снег" },
  75: { emoji: "❄️", text: "Сильный снег" },
  77: { emoji: "❄️", text: "Снежные зёрна" },
  80: { emoji: "🌦️", text: "Небольшой ливень" },
  81: { emoji: "🌧️", text: "Ливень" },
  82: { emoji: "⛈️", text: "Сильный ливень" },
  85: { emoji: "🌨️", text: "Снегопад" },
  86: { emoji: "❄️", text: "Сильный снегопад" },
  95: { emoji: "⛈️", text: "Гроза" },
  96: { emoji: "⛈️", text: "Гроза с градом" },
  99: { emoji: "⛈️", text: "Сильная гроза с градом" },
};

function codeInfo(code) {
  return WEATHER_CODES[code] || { emoji: "🌡️", text: "Неизвестно" };
}

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("error", isError);
}

function roundTemp(value) {
  return `${Math.round(value)}°`;
}

// Дата "2026-11-08" → "сб, 08.11" без сюрпризов часовых поясов
function formatDay(dateString) {
  const [year, month, day] = dateString.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  const weekdays = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
  const dd = String(day).padStart(2, "0");
  const mm = String(month).padStart(2, "0");
  return `${weekdays[date.getDay()]}, ${dd}.${mm}`;
}

async function getWeather(city) {
  const query = city.trim();
  if (!query) {
    setStatus("Напиши город 🙂", true);
    return;
  }

  weatherBtn.disabled = true;
  cardEl.classList.add("hidden");
  setStatus("Ищу город…");

  try {
    // Шаг 1: город → координаты
    const geoUrl =
      "https://geocoding-api.open-meteo.com/v1/search" +
      `?name=${encodeURIComponent(query)}&count=1&language=ru&format=json`;

    const geoResponse = await fetch(geoUrl);
    if (!geoResponse.ok) throw new Error("Геокодер недоступен");
    const geo = await geoResponse.json();

    if (!geo.results || geo.results.length === 0) {
      setStatus(`Город «${query}» не найден 😕 Попробуй иначе.`, true);
      return;
    }

    const place = geo.results[0];
    setStatus("Загружаю погоду…");

    // Шаг 2: координаты → погода
    const weatherUrl =
      "https://api.open-meteo.com/v1/forecast" +
      `?latitude=${place.latitude}&longitude=${place.longitude}` +
      "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m" +
      "&daily=weather_code,temperature_2m_max,temperature_2m_min" +
      "&timezone=auto&forecast_days=3";

    const weatherResponse = await fetch(weatherUrl);
    if (!weatherResponse.ok) throw new Error("Сервис погоды недоступен");
    const data = await weatherResponse.json();

    const current = data.current;
    const info = codeInfo(current.weather_code);

    document.getElementById("w-city").textContent =
      `${place.name}${place.country ? ", " + place.country : ""}`;
    document.getElementById("w-emoji").textContent = info.emoji;
    document.getElementById("w-temp").textContent = roundTemp(current.temperature_2m);
    document.getElementById("w-desc").textContent = info.text;
    document.getElementById("w-feels").textContent = roundTemp(current.apparent_temperature);
    document.getElementById("w-humidity").textContent = `${current.relative_humidity_2m}%`;
    document.getElementById("w-wind").textContent = `${Math.round(current.wind_speed_10m)} км/ч`;

    // Прогноз на 3 дня
    const forecastEl = document.getElementById("w-forecast");
    forecastEl.innerHTML = "";

    data.daily.time.forEach((day, index) => {
      const dayInfo = codeInfo(data.daily.weather_code[index]);
      const block = document.createElement("div");
      block.className = "forecast-day";
      block.innerHTML =
        `<div class="f-date">${formatDay(day)}</div>` +
        `<span class="f-emoji">${dayInfo.emoji}</span>` +
        `<div class="f-temp">${roundTemp(data.daily.temperature_2m_max[index])} / ` +
        `${roundTemp(data.daily.temperature_2m_min[index])}</div>`;
      forecastEl.appendChild(block);
    });

    cardEl.classList.remove("hidden");
    setStatus("");

    // Запоминаем город, чтобы в следующий раз открыть сразу его
    try {
      localStorage.setItem("lastCity", place.name);
    } catch (e) {
      /* localStorage может быть недоступен — не страшно */
    }
  } catch (error) {
    console.error(error);
    setStatus("Не получилось загрузить погоду 🛠️ Проверь интернет и попробуй ещё раз.", true);
  } finally {
    weatherBtn.disabled = false;
  }
}

weatherBtn.addEventListener("click", () => getWeather(cityInput.value));
cityInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") getWeather(cityInput.value);
});

// При открытии: берём сохранённый город или Москву
let savedCity = "Москва";
try {
  savedCity = localStorage.getItem("lastCity") || "Москва";
} catch (e) {
  /* ignore */
}
cityInput.value = savedCity;
getWeather(savedCity);
