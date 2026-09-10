/* Логика окна.
 *
 * Три вещи, которые здесь важнее остального:
 *  - объект бриджа называется window.WebApp, а не Telegram.WebApp, и вне MAX
 *    его нет вообще (локально в браузере всё работает без него);
 *  - на экране ожидания кнопку «назад» НЕ занимаем: незанятую обрабатывает
 *    сам MAX и закрывает окно, а другого способа его закрыть в API нет;
 *  - при открытии окна первым делом спрашиваем /api/state: человек мог
 *    закрыть окно в середине генерации и вернуться.
 *
 * Чего здесь больше НЕТ: обмена файлами с чатом. Окну MAX не отдаёт ни
 * микрофон, ни камеру, и раньше запись заказывалась боту — окно ждало,
 * человек уходил в переписку, снимал, возвращался по кнопке. Шесть
 * действий на одно поле. Теперь весь путь со своим голосом целиком идёт
 * в переписке, а окно работает только с тем, что можно выбрать прямо
 * здесь: готовый голос или файл.
 */
(function () {
  "use strict";

  var WA = window.WebApp || null;
  var INIT = WA && WA.initData ? WA.initData : "";
  var voice = { mode: "preset", id: "" };
  var pickedVideo = null;
  // Экран photo обслуживает два режима — обычный аватар и рисованный.
  // Вход у них одинаковый, различается только то, что делает сервер,
  // поэтому держим один экран и одну переменную вместо двух копий разметки.
  var photoMode = "photo";
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

  function setPhotoMode(mode) {
    photoMode = mode === "toon" ? "toon" : "photo";
    var toon = photoMode === "toon";
    document.getElementById("photo-title").textContent =
      toon ? "Cartoon avatar" : "Аватар по фото";
    document.getElementById("toon-lead").hidden = !toon;
    document.getElementById("photo-go").textContent =
      toon ? "Нарисовать и оживить" : "Сделать аватара";
  }

  // --- готовность формы -----------------------------------------------------
  // Кнопка неактивна, пока не заполнено всё. Так честнее, чем ошибка после
  // нажатия: человек видит, что чего-то не хватает, ещё до того как ткнул,
  // и не гадает, почему «сделать» ничего не сделало.

  function voiceReady() {
    if (voice.mode === "preset") return !!voice.id;
    if (voice.mode === "file") return !!document.getElementById("voice-file").files[0];
    // Вкладка «свой голос» — это объяснение, а не поле ввода: записать
    // голос в окне MAX не даёт, весь такой путь идёт в переписке.
    return false;
  }

  function refreshGo() {
    var photoOk = !!document.getElementById("photo-file").files[0]
                && voiceReady()
                && !!document.getElementById("photo-text").value.trim();
    document.querySelector('[data-submit="photo"]').disabled = !photoOk;

    var videoOk = !!pickedVideo
                && !!document.getElementById("video-text").value.trim();
    document.querySelector('[data-submit="video"]').disabled = !videoOk;
  }

  // --- счётчик секунд -------------------------------------------------------
  // Показываем секунды, а не символы: ограничивает нас длительность речи,
  // а не длина строки, и человеку так понятнее, почему нельзя больше.

  function wireCounter(id) {
    var area = document.getElementById(id);
    var out = document.querySelector('[data-counter="' + id + '"]');
    var timer = null;
    area.addEventListener("input", function () {
      // Готовность считаем сразу, без задержки: кнопка должна оживать
      // на первом же символе, а не через четверть секунды.
      refreshGo();
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (!area.value.trim()) { out.textContent = " "; out.className = "counter"; return; }
        api("/api/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: area.value })
        }).then(function (r) {
          // Одно сообщение и не больше одного числа. Раньше здесь было
          // «≈ 0.4 с из 14 · ролик 4 с — короче пяти секунд менее
          // стабильны»: четыре числа в строке, и человек читает это как
          // ошибку, хотя всё в порядке. Знать ему надо одно из трёх —
          // всё хорошо, коротко или длинно.
          if (r.over) {
            out.textContent = "Длинно — не влезет. Сократите примерно на "
                            + Math.ceil(r.speech_seconds - r.limit_seconds) + " с";
            out.className = "counter over";
          } else if (r.short) {
            out.textContent = "Коротко — такой ролик выходит менее стабильным";
            out.className = "counter warn";
          } else {
            out.textContent = "≈ " + Math.round(r.speech_seconds) + " с";
            out.className = "counter";
          }
        }).catch(function () { out.textContent = " "; });
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
        refreshGo();
      });
    }

    document.getElementById("voice-file").addEventListener("change", refreshGo);

    api("/api/voices").then(function (r) {
      var box = document.getElementById("voice-list");
      box.innerHTML = "";
      if (!r.voices.length) {
        box.innerHTML = '<div class="hint">Готовых голосов пока нет — '
                      + 'загрузите файл или сделайте аватара в боте.</div>';
        refreshGo();
        return;
      }
      r.voices.forEach(function (v, n) {
        var b = document.createElement("button");
        b.className = "chip" + (n === 0 ? " on" : "");
        b.innerHTML = "<b>" + v.title + "</b><small>" + v.note + "</small>";
        b.addEventListener("click", function () {
          var all = box.querySelectorAll(".chip");
          for (var i = 0; i < all.length; i++) all[i].classList.remove("on");
          b.classList.add("on");
          voice.id = v.id;
          refreshGo();
        });
        if (n === 0) voice.id = v.id;
        box.appendChild(b);
      });
      refreshGo();
    });
  }

  // --- видео ----------------------------------------------------------------

  function wireVideo() {
    var input = document.getElementById("video-file");
    input.addEventListener("change", function () {
      pickedVideo = input.files[0] || null;
      showVideoState();
    });
    document.querySelectorAll("[data-drop]").forEach(function (b) {
      b.addEventListener("click", function () {
        pickedVideo = null;
        input.value = "";
        showVideoState();
      });
    });
  }

  function showVideoState() {
    var slot = document.getElementById("video-slot");
    slot.hidden = !pickedVideo;
    if (pickedVideo) slot.querySelector(".slot-text").textContent = "Выбрано: " + pickedVideo.name;
    refreshGo();
  }

  // --- отправка -------------------------------------------------------------

  function submit(mode) {
    var err = document.querySelector('[data-err="' + mode + '"]');
    err.textContent = "";
    var fd = new FormData();
    // Экран называется photo, а режимов у него два: сервер должен получить
    // именно режим, иначе рисованный аватар молча станет обычным.
    fd.append("mode", mode === "photo" ? photoMode : mode);
    fd.append("text", document.getElementById(mode + "-text").value);

    if (mode === "photo") {
      fd.append("photo", document.getElementById("photo-file").files[0]);
      if (voice.mode === "preset") {
        fd.append("voice_id", voice.id);
      } else {
        fd.append("voice", document.getElementById("voice-file").files[0]);
      }
    } else {
      fd.append("video", pickedVideo);
    }

    var btn = document.querySelector('[data-submit="' + mode + '"]');
    btn.disabled = true;
    api("/api/avatar", { method: "POST", body: fd })
      .then(function (job) { resume(job); })
      .catch(function (e) {
        refreshGo();
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
    var toon = job.mode === "toon";

    // Портрет появляется на середине пути и остаётся до конца: пока идёт
    // видео — как «уже что-то есть», после — как обложка результата.
    var poster = document.getElementById("wait-poster");
    if (job.poster_url && poster.hidden) {
      document.getElementById("wait-poster-img").src = job.poster_url;
      poster.hidden = false;
    }

    if (job.status === "queued") {
      title.textContent = "В очереди";
      note.textContent = job.queue_position
        ? "Впереди задач: " + job.queue_position
        : "Скоро начну";
      bar.style.width = "4%";
      return;
    }
    if (job.status === "running") {
      title.textContent = toon ? "Делаю рисованного аватара" : "Делаю аватара";
      if (toon) {
        note.textContent = job.poster_url
          ? "Портрет готов, оживляю — ещё около полуминуты"
          : "Рисую портрет, это секунд двадцать";
      } else {
        note.textContent = "Обычно это от тридцати секунд до полутора минут";
      }
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
        // Ролик готов — портрет своё отработал и только занимает экран.
        document.getElementById("wait-poster").hidden = true;
      }
    } else {
      title.textContent = "Не получилось";
      note.textContent = job.error || "Попробуйте ещё раз через пару минут";
      bar.style.width = "0";
    }
  }

  // --- старт ----------------------------------------------------------------

  document.querySelectorAll("[data-go]").forEach(function (b) {
    b.addEventListener("click", function () {
      var where = b.getAttribute("data-go");
      if (where === "photo") setPhotoMode(b.getAttribute("data-mode"));
      show(where);
      refreshGo();
    });
  });
  document.querySelectorAll("[data-submit]").forEach(function (b) {
    b.addEventListener("click", function () { submit(b.getAttribute("data-submit")); });
  });
  document.getElementById("photo-file").addEventListener("change", refreshGo);
  wireCounter("photo-text");
  wireCounter("video-text");
  wireVoice();
  wireVideo();
  refreshGo();

  api("/api/state").then(function (state) {
    // Кнопку рисуем, только если сервер сказал, что рисовать есть чем.
    // Без ключей Kandinsky режим отказал бы уже после нажатия.
    if (state.toon) document.getElementById("card-toon").hidden = false;
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
