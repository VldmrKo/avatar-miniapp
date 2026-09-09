/* Логика окна.
 *
 * Три вещи, которые здесь важнее остального:
 *  - объект бриджа называется window.WebApp, а не Telegram.WebApp, и вне MAX
 *    его нет вообще (локально в браузере всё работает без него);
 *  - на экране ожидания кнопку «назад» НЕ занимаем: незанятую обрабатывает
 *    сам MAX и закрывает окно, а другого способа его закрыть в API нет;
 *  - при открытии окна первым делом спрашиваем /api/state: человек мог
 *    закрыть окно в середине генерации и вернуться.
 */
(function () {
  "use strict";

  var WA = window.WebApp || null;
  var INIT = WA && WA.initData ? WA.initData : "";
  var voice = { mode: "preset", id: "", blob: null };
  var poll = null;
  var screen = "boot";

  // --- сеть -----------------------------------------------------------------

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["X-Init-Data"] = INIT;
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.ok) return data;
        var err = new Error(data.error || ("HTTP " + r.status));
        err.status = r.status;   // 409 — это не ошибка, а «у тебя уже идёт»
        err.data = data;
        throw err;
      });
    });
  }

  // --- экраны ---------------------------------------------------------------

  function show(name) {
    screen = name;
    var all = document.querySelectorAll(".screen");
    for (var i = 0; i < all.length; i++) all[i].classList.remove("on");
    var el = document.getElementById(name);
    if (el) el.classList.add("on");

    if (!WA || !WA.BackButton) return;
    if (name === "photo" || name === "video") {
      WA.BackButton.show();
    } else {
      // На pick и на ожидании кнопку отдаём системе: там она закрывает окно.
      WA.BackButton.hide();
    }
  }

  if (WA && WA.BackButton && WA.BackButton.onClick) {
    WA.BackButton.onClick(function () {
      // Событие на части платформ прилетает и при скрытой кнопке,
      // поэтому смотрим текущий экран, а не доверяем видимости.
      if (screen === "photo" || screen === "video") show("pick");
    });
  }

  // --- счётчик секунд -------------------------------------------------------
  // Показываем секунды, а не символы: ограничивает нас длительность речи,
  // а не длина строки, и человеку так понятнее, почему нельзя больше.

  function wireCounter(id) {
    var area = document.getElementById(id);
    var out = document.querySelector('[data-counter="' + id + '"]');
    var timer = null;
    area.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (!area.value.trim()) { out.textContent = " "; out.className = "counter"; return; }
        api("/api/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: area.value })
        }).then(function (r) {
          out.textContent = "≈ " + r.speech_seconds + " с из " + r.limit_seconds;
          out.className = r.over ? "counter over" : "counter";
        }).catch(function () { out.textContent = " "; });
      }, 250);
    });
  }

  // --- голос ----------------------------------------------------------------

  function wireVoice() {
    var tabs = document.querySelectorAll("[data-voice-tab]");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function () {
        var name = this.getAttribute("data-voice-tab");
        voice.mode = name;
        var all = document.querySelectorAll("[data-voice-tab]");
        for (var j = 0; j < all.length; j++) all[j].classList.remove("on");
        this.classList.add("on");
        var bodies = document.querySelectorAll("[data-voice-body]");
        for (var k = 0; k < bodies.length; k++) {
          bodies[k].classList.toggle("on", bodies[k].getAttribute("data-voice-body") === name);
        }
      });
    }

    api("/api/voices").then(function (r) {
      var box = document.getElementById("voice-list");
      box.innerHTML = "";
      r.voices.forEach(function (v, n) {
        var b = document.createElement("button");
        b.className = "chip" + (n === 0 ? " on" : "");
        b.innerHTML = "<b>" + v.title + "</b><small>" + v.note + "</small>";
        b.addEventListener("click", function () {
          var all = box.querySelectorAll(".chip");
          for (var i = 0; i < all.length; i++) all[i].classList.remove("on");
          b.classList.add("on");
          voice.id = v.id;
        });
        if (n === 0) voice.id = v.id;
        box.appendChild(b);
      });
    });

    // Запись показываем только если она в этом вебвью вообще возможна.
    var canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
                       && window.MediaRecorder);
    if (canRecord) document.querySelector('[data-voice-tab="record"]').hidden = false;
    else return;

    var btn = document.getElementById("rec-btn");
    var state = document.getElementById("rec-state");
    btn.addEventListener("click", function () {
      btn.disabled = true;
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        var chunks = [];
        var rec = new MediaRecorder(stream);
        rec.ondataavailable = function (e) { chunks.push(e.data); };
        rec.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          voice.blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
          state.textContent = "Записано. Можно перезаписать.";
          btn.disabled = false;
        };
        rec.start();
        var left = 5;
        state.textContent = "Говорите… 5";
        var tick = setInterval(function () {
          left -= 1;
          state.textContent = "Говорите… " + left;
          if (left <= 0) { clearInterval(tick); rec.stop(); }
        }, 1000);
      }).catch(function (e) {
        state.textContent = "Микрофон недоступен: " + e.name;
        btn.disabled = false;
      });
    });
  }

  // --- отправка -------------------------------------------------------------

  function submit(mode) {
    var err = document.querySelector('[data-err="' + mode + '"]');
    err.textContent = "";
    var fd = new FormData();
    fd.append("mode", mode);
    fd.append("text", document.getElementById(mode + "-text").value);

    if (mode === "photo") {
      var photo = document.getElementById("photo-file").files[0];
      if (!photo) { err.textContent = "Нужно фото."; return; }
      fd.append("photo", photo);
      if (voice.mode === "preset") {
        fd.append("voice_id", voice.id);
      } else if (voice.mode === "record") {
        if (!voice.blob) { err.textContent = "Запишите голос или выберите готовый."; return; }
        fd.append("voice", voice.blob, "voice.webm");
      } else {
        var vf = document.getElementById("voice-file").files[0];
        if (!vf) { err.textContent = "Выберите файл с голосом."; return; }
        fd.append("voice", vf);
      }
    } else {
      var vid = document.getElementById("video-file").files[0];
      if (!vid) { err.textContent = "Нужно видео."; return; }
      fd.append("video", vid);
    }

    var btn = document.querySelector('[data-submit="' + mode + '"]');
    btn.disabled = true;
    api("/api/avatar", { method: "POST", body: fd })
      .then(function (job) { btn.disabled = false; resume(job); })
      .catch(function (e) {
        btn.disabled = false;
        if (e.status === 409) { resume(e.data); return; }   // уже идёт — это прогресс
        err.textContent = e.message;
      });
  }

  // --- ожидание -------------------------------------------------------------

  function resume(job) {
    show("wait");
    render(job);
    clearInterval(poll);
    poll = setInterval(function () {
      api("/api/jobs/" + job.job_id).then(render).catch(function () {});
    }, 3000);
  }

  function render(job) {
    var bar = document.getElementById("wait-bar");
    var note = document.getElementById("wait-note");
    var title = document.getElementById("wait-title");

    if (job.status === "queued") {
      title.textContent = "В очереди";
      note.textContent = job.queue_position
        ? "Впереди задач: " + job.queue_position
        : "Скоро начну";
      bar.style.width = "4%";
      return;
    }
    if (job.status === "running") {
      title.textContent = "Делаю аватара";
      note.textContent = "Обычно это от тридцати секунд до полутора минут";
      bar.style.width = Math.max(6, job.progress) + "%";
      return;
    }

    clearInterval(poll);
    if (job.status === "done") {
      title.textContent = "Готово";
      note.textContent = "Ролик уже отправлен вам в чат";
      bar.style.width = "100%";
      if (job.media_url) {
        var v = document.getElementById("wait-video");
        v.src = job.media_url;
        v.hidden = false;
      }
    } else {
      title.textContent = "Не получилось";
      note.textContent = job.error || "Попробуйте ещё раз через пару минут";
      bar.style.width = "0";
    }
  }

  // --- старт ----------------------------------------------------------------

  document.querySelectorAll("[data-go]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-go")); });
  });
  document.querySelectorAll("[data-submit]").forEach(function (b) {
    b.addEventListener("click", function () { submit(b.getAttribute("data-submit")); });
  });
  wireCounter("photo-text");
  wireCounter("video-text");
  wireVoice();

  api("/api/state").then(function (state) {
    if (state.job) { resume(state.job); return; }
    show("pick");
  }).catch(function (e) {
    // Разделяем случаи: мост MAX не загрузился, подписи в окне нет, подпись
    // не принята сервером. Лечатся они по-разному, и без этой развилки на
    // три разные поломки видно одинаковое «не удалось».
    var why;
    if (!WA) {
      why = "Мост MAX не загрузился — подпись брать неоткуда. "
          + "Вне MAX это нормально, внутри — проверьте, что страница отдала max-web-app.js.";
    } else if (!INIT) {
      why = "MAX не передал подпись этому окну.";
    } else if (e.status === 401) {
      why = "MAX не принял подпись. Обычно это значит, что адрес приложения привязан "
          + "к другому боту, чем тот, чьим токеном она проверяется.";
    } else {
      why = e.message;
    }
    document.getElementById("boot").innerHTML =
      '<p class="err">' + why + "</p>"
      + '<button class="wide" id="boot-retry">Повторить</button>';
    var retry = document.getElementById("boot-retry");
    if (retry) retry.addEventListener("click", function () { location.reload(); });
  });
})();
