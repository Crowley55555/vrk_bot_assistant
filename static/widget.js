/**
 * Виджет чат-бота ООО "Завод ВРК".
 *
 * Самодостаточный скрипт: инжектит все DOM-элементы и CSS,
 * отправляет сообщения на /api/chat, обрабатывает ответы.
 *
 * ── ИНСТРУКЦИЯ ПО ВСТАВКЕ ──
 * Добавьте перед </body> на любой HTML-странице:
 *
 *   <link rel="stylesheet" href="https://YOUR_SERVER/static/widget.css">
 *   <script src="https://YOUR_SERVER/static/widget.js"
 *           data-api="https://YOUR_SERVER"></script>
 *
 * Атрибут data-api указывает базовый URL бэкенда (без /api/chat).
 * Если атрибут не задан, используется тот же origin, что и у скрипта.
 */

(function () {
    "use strict";

    /* ─── Конфигурация ──────────────────────────────────────────────── */

    var scriptTag = document.currentScript;
    var API_BASE = (scriptTag && scriptTag.getAttribute("data-api")) || "";
    var API_URL = API_BASE + "/api/chat";

    var SESSION_KEY = "vrk_chat_session_id";

    function getSessionId() {
        var id = localStorage.getItem(SESSION_KEY);
        if (!id) {
            id = "web_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
            localStorage.setItem(SESSION_KEY, id);
        }
        return id;
    }

    /* ─── DOM-генерация ─────────────────────────────────────────────── */

    function createWidget() {
        // Плавающая кнопка
        var btn = document.createElement("button");
        btn.className = "vrk-chat-btn";
        btn.setAttribute("aria-label", "Открыть чат");
        btn.innerHTML =
            '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1' +
            ' 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>';
        document.body.appendChild(btn);

        // Окно чата
        var win = document.createElement("div");
        win.className = "vrk-chat-window";
        win.innerHTML =
            '<div class="vrk-chat-header">' +
            '  <div class="vrk-chat-header__avatar">🏭</div>' +
            '  <div class="vrk-chat-header__info">' +
            '    <div class="vrk-chat-header__title">Завод ВРК</div>' +
            '    <div class="vrk-chat-header__subtitle">Бот-консультант</div>' +
            "  </div>" +
            '  <button class="vrk-chat-header__close" aria-label="Закрыть">&times;</button>' +
            "</div>" +
            '<div class="vrk-chat-messages" id="vrk-messages"></div>' +
            '<div class="vrk-buttons" id="vrk-buttons"></div>' +
            '<div class="vrk-chat-input">' +
            '  <input class="vrk-chat-input__field" id="vrk-input" placeholder="Напишите сообщение…" />' +
            '  <button class="vrk-chat-input__send" id="vrk-send" aria-label="Отправить">' +
            '    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>' +
            "  </button>" +
            "</div>";
        document.body.appendChild(win);

        // Элементы
        var messagesEl = document.getElementById("vrk-messages");
        var buttonsEl = document.getElementById("vrk-buttons");
        var inputEl = document.getElementById("vrk-input");
        var sendBtn = document.getElementById("vrk-send");
        var closeBtn = win.querySelector(".vrk-chat-header__close");

        var isOpen = false;

        // ── Открыть / закрыть ──
        function toggle() {
            isOpen = !isOpen;
            win.classList.toggle("vrk-chat-window--open", isOpen);
            if (isOpen && messagesEl.children.length === 0) {
                addBotMessage("Здравствуйте! Я бот-консультант ООО «Завод ВРК». Чем могу помочь?\n\nНажмите «Начать подбор» или задайте вопрос.");
                addButtons([
                    { label: "▶️ Начать подбор", value: "Старт" },
                ]);
            }
            if (isOpen) inputEl.focus();
        }

        btn.addEventListener("click", toggle);
        closeBtn.addEventListener("click", toggle);

        // ── Сообщения ──
        function addMessage(text, cls) {
            var div = document.createElement("div");
            div.className = "vrk-msg " + cls;
            div.textContent = text;
            messagesEl.appendChild(div);
            messagesEl.scrollTop = messagesEl.scrollHeight;
            return div;
        }

        function addBotMessage(text) {
            return addMessage(text, "vrk-msg--bot");
        }

        function addUserMessage(text) {
            return addMessage(text, "vrk-msg--user");
        }

        function addTyping() {
            return addMessage("Печатает…", "vrk-msg--typing");
        }

        function removeTyping(el) {
            if (el && el.parentNode) el.parentNode.removeChild(el);
        }

        // ── Карточка товара ──
        function addProductCard(data) {
            var card = document.createElement("div");
            card.className = "vrk-product-card";
            var html = "";
            if (data.name) html += '<div class="vrk-product-card__name">' + escHtml(data.name) + "</div>";
            if (data.article) html += '<div class="vrk-product-card__detail">Арт. ' + escHtml(data.article) + "</div>";
            if (data.category) html += '<div class="vrk-product-card__detail">' + escHtml(data.category) + "</div>";
            if (data.price) html += '<div class="vrk-product-card__price">' + escHtml(data.price) + "</div>";
            if (data.url) html += '<a class="vrk-product-card__link" href="' + escAttr(data.url) + '" target="_blank" rel="noopener">Открыть на сайте →</a>';
            card.innerHTML = html;
            messagesEl.appendChild(card);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        // ── Кнопки ──
        function addButtons(buttons) {
            buttonsEl.innerHTML = "";
            if (!buttons || !buttons.length) return;
            buttons.forEach(function (b) {
                var btnEl = document.createElement("button");
                btnEl.className = "vrk-buttons__btn";
                btnEl.textContent = b.label;
                btnEl.addEventListener("click", function () {
                    sendMessage(b.value);
                });
                buttonsEl.appendChild(btnEl);
            });
        }

        function clearButtons() {
            buttonsEl.innerHTML = "";
        }

        // ── Отправка ──
        var sending = false;

        async function sendMessage(text) {
            if (sending || !text.trim()) return;
            sending = true;
            sendBtn.disabled = true;
            clearButtons();

            addUserMessage(text);
            inputEl.value = "";

            var typingEl = addTyping();

            try {
                var resp = await fetch(API_URL, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        message: text,
                        session_id: getSessionId(),
                        source: "web",
                    }),
                });

                var data = await resp.json();
                removeTyping(typingEl);

                if (data.reply) addBotMessage(data.reply);

                if (data.action === "show_product" && data.product_data) {
                    addProductCard(data.product_data);
                }

                if (data.buttons && data.buttons.length) {
                    addButtons(data.buttons);
                }
            } catch (err) {
                removeTyping(typingEl);
                addBotMessage("Ошибка связи с сервером. Попробуйте позже или позвоните: +7 (800) 505-63-73");
                console.error("VRK Chat error:", err);
            }

            sending = false;
            sendBtn.disabled = false;
        }

        // ── События ввода ──
        sendBtn.addEventListener("click", function () {
            sendMessage(inputEl.value);
        });
        inputEl.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage(inputEl.value);
            }
        });
    }

    /* ─── Утилиты ───────────────────────────────────────────────────── */

    function escHtml(s) {
        var d = document.createElement("div");
        d.appendChild(document.createTextNode(s));
        return d.innerHTML;
    }

    function escAttr(s) {
        return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    /* ─── Инициализация ─────────────────────────────────────────────── */

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", createWidget);
    } else {
        createWidget();
    }
})();
