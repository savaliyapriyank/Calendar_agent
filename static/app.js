// Mindlytic AI Calendar - Chat Dashboard Controller

document.addEventListener('DOMContentLoaded', () => {
  // Configure Marked for GitHub flavored markdown with hard line breaks
  if (window.marked && window.marked.setOptions) {
    window.marked.setOptions({
      breaks: true,
      gfm: true,
    });
  }

  // Initialize Lucide Icons
  if (window.lucide) {
    window.lucide.createIcons();
  }

  // --- 1. Theme Management ---
  const themeToggleBtn = document.getElementById('theme-toggle');
  const htmlRoot = document.documentElement;

  function updateThemeButton(theme) {
    if (!themeToggleBtn) return;
    const isDark = theme === 'dark';
    const iconName = isDark ? 'moon' : 'sun';
    themeToggleBtn.setAttribute('title', isDark ? 'Switch to Light Theme' : 'Switch to Dark Theme');
    themeToggleBtn.innerHTML = `<i data-lucide="${iconName}" class="w-4 h-4"></i>`;
    if (window.lucide) {
      window.lucide.createIcons();
    }
  }

  function setTheme(theme) {
    htmlRoot.setAttribute('data-theme', theme);
    localStorage.setItem('mindlytic_calendar_theme', theme);
    updateThemeButton(theme);
  }

  const savedTheme = localStorage.getItem('mindlytic_calendar_theme') || localStorage.getItem('gemini_calendar_theme') || 'dark';
  setTheme(savedTheme);

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', () => {
      const current = htmlRoot.getAttribute('data-theme');
      setTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // --- 2. Chat Elements & State ---
  const scrollContainer = document.getElementById('chat-scroll-container');
  const chatMessages = document.getElementById('chat-messages');
  const heroState = document.getElementById('hero-state');
  const chatForm = document.getElementById('chat-form');
  const chatInput = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  const thinkingIndicator = document.getElementById('thinking-indicator');
  const newChatBtn = document.getElementById('new-chat-btn');
  const scrollToBottomBtn = document.getElementById('scroll-to-bottom-btn');

  let conversationHistory = [];

  function updateScrollButton() {
    if (!scrollContainer || !scrollToBottomBtn) return;
    const distanceFromBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
    if (distanceFromBottom > 100) {
      scrollToBottomBtn.classList.add('visible');
    } else {
      scrollToBottomBtn.classList.remove('visible');
    }
  }

  function scrollToBottom(smooth = false) {
    if (scrollContainer) {
      if (smooth) {
        scrollContainer.scrollTo({
          top: scrollContainer.scrollHeight,
          behavior: 'smooth'
        });
      } else {
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
      }
    }
    setTimeout(updateScrollButton, 100);
  }

  if (scrollContainer) {
    scrollContainer.addEventListener('scroll', updateScrollButton);
  }

  if (scrollToBottomBtn) {
    scrollToBottomBtn.addEventListener('click', () => {
      scrollToBottom(true);
      chatInput?.focus();
    });
  }

  // Adjust input textarea height dynamically
  if (chatInput) {
    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      const newHeight = Math.min(chatInput.scrollHeight, 140);
      chatInput.style.height = `${newHeight}px`;

      // Toggle send button disabled state
      const hasText = chatInput.value.trim().length > 0;
      if (sendBtn) {
        sendBtn.disabled = !hasText;
      }
    });

    // Handle Enter vs Shift+Enter
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (chatInput.value.trim().length > 0) {
          chatForm?.dispatchEvent(new Event('submit'));
        }
      }
    });
  }

  // Click handler for Hero suggestion chips
  if (heroState) {
    heroState.addEventListener('click', (e) => {
      const chip = e.target.closest('.suggestion-chip');
      if (chip) {
        const prompt = chip.getAttribute('data-prompt');
        if (prompt) {
          handleSendMessage(prompt);
        }
      }
    });
  }

  function attachCopyHandler(btn, textToCopy) {
    let timeoutId = null;

    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      e.preventDefault();

      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(textToCopy);
        } else {
          const textarea = document.createElement('textarea');
          textarea.value = textToCopy;
          textarea.style.position = 'fixed';
          textarea.style.left = '-9999px';
          textarea.style.top = '-9999px';
          textarea.style.opacity = '0';
          document.body.appendChild(textarea);
          textarea.focus();
          textarea.select();
          document.execCommand('copy');
          document.body.removeChild(textarea);
        }

        btn.classList.add('copied');
        btn.setAttribute('title', 'Copied!');
        btn.innerHTML = `<i data-lucide="check" class="w-3.5 h-3.5 text-emerald-400"></i>`;
        if (window.lucide) window.lucide.createIcons();

        if (timeoutId) clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
          btn.classList.remove('copied');
          btn.setAttribute('title', 'Copy message');
          btn.innerHTML = `<i data-lucide="copy" class="w-3.5 h-3.5"></i>`;
          if (window.lucide) window.lucide.createIcons();
        }, 2000);
      } catch (err) {
        console.error('Failed to copy to clipboard:', err);
      }
    });
  }

  function appendUserMessage(text) {
    if (heroState) {
      heroState.classList.add('hidden');
    }

    const trimmed = text.trim();
    const msgWrapper = document.createElement('div');
    msgWrapper.className = 'chat-message-row user-row flex flex-col items-end w-full animate-fade-in my-2 group';
    
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble-user';
    bubble.textContent = trimmed;

    const actionRow = document.createElement('div');
    actionRow.className = 'flex justify-end pt-1 pr-1 min-h-[24px]';

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'copy-msg-btn user-copy';
    copyBtn.title = 'Copy message';
    copyBtn.setAttribute('aria-label', 'Copy message');
    copyBtn.innerHTML = `<i data-lucide="copy" class="w-3.5 h-3.5"></i>`;

    attachCopyHandler(copyBtn, trimmed);

    actionRow.appendChild(copyBtn);
    msgWrapper.appendChild(bubble);
    msgWrapper.appendChild(actionRow);
    chatMessages.appendChild(msgWrapper);
    if (window.lucide) window.lucide.createIcons();
    scrollToBottom();
  }

  function appendAssistantMessage(markdownText, actions = []) {
    let actionsHtml = '';
    if (actions && actions.length > 0) {
      const renderedLabels = new Set();
      actions.forEach((act) => {
        let label = 'Action Completed';
        let icon = 'check-circle';
        let badgeClass = '';
        const toolName = act.tool || '';
        
        if (toolName.includes('create')) {
          label = 'Event Created';
          icon = 'calendar-plus';
          badgeClass = 'badge-success';
        } else if (toolName.includes('update')) {
          label = 'Event Updated';
          icon = 'calendar';
          badgeClass = 'badge-info';
        } else if (toolName.includes('delete')) {
          label = 'Event Deleted';
          icon = 'calendar-x';
          badgeClass = 'badge-warn';
        } else if (toolName.includes('send_email')) {
          label = 'Email Sent';
          icon = 'mail';
          badgeClass = 'badge-success';
        } else if (toolName.includes('freebusy')) {
          label = 'Checked Availability';
          icon = 'clock';
          badgeClass = 'badge-info';
        } else if (toolName.includes('list_events') || toolName.includes('search_events')) {
          label = 'Fetched Schedule';
          icon = 'calendar-days';
          badgeClass = 'badge-info';
        }

        if (!renderedLabels.has(label)) {
          renderedLabels.add(label);
          actionsHtml += `
            <div class="action-badge ${badgeClass}">
              <i data-lucide="${icon}" class="w-3.5 h-3.5"></i>
              <span>${label}</span>
            </div>
          `;
        }
      });
    }

    const htmlContent = window.marked ? window.marked.parse(markdownText) : markdownText;

    const msgWrapper = document.createElement('div');
    msgWrapper.className = 'chat-message-row assistant-row flex gap-3.5 w-full animate-fade-in items-start my-3 group';
    msgWrapper.innerHTML = `
      <div class="w-8 h-8 rounded-xl bg-gradient-to-tr from-indigo-500 via-purple-500 to-pink-500 flex items-center justify-center shrink-0 shadow-md shadow-indigo-500/20 border border-white/10 mt-0.5">
        <i data-lucide="sparkles" class="w-4 h-4 text-white"></i>
      </div>
      <div class="flex-1 max-w-[92%] space-y-1.5">
        <div class="chat-bubble-assistant prose-chat">
          ${htmlContent}
        </div>
        <div class="msg-footer-bar flex items-center gap-2 mt-1.5 min-h-[26px]">
          ${actionsHtml ? `<div class="flex flex-wrap gap-2">${actionsHtml}</div>` : ''}
          <button type="button" class="copy-msg-btn assistant-copy" title="Copy response" aria-label="Copy response">
            <i data-lucide="copy" class="w-3.5 h-3.5"></i>
          </button>
        </div>
      </div>
    `;

    const copyBtn = msgWrapper.querySelector('.copy-msg-btn');
    if (copyBtn) {
      attachCopyHandler(copyBtn, markdownText);
    }

    chatMessages.appendChild(msgWrapper);
    if (window.lucide) window.lucide.createIcons();
    scrollToBottom();
  }

  async function handleSendMessage(text) {
    if (!text || !text.trim()) return;

    appendUserMessage(text);
    conversationHistory.push({ role: 'user', content: text });

    if (chatInput) {
      chatInput.value = '';
      chatInput.style.height = 'auto';
    }
    if (sendBtn) sendBtn.disabled = true;
    if (thinkingIndicator) thinkingIndicator.classList.remove('hidden');
    scrollToBottom();

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: conversationHistory,
        }),
      });

      const data = await response.json();
      if (thinkingIndicator) thinkingIndicator.classList.add('hidden');
      if (sendBtn) sendBtn.disabled = true;

      if (data.success) {
        appendAssistantMessage(data.response, data.actions);
        conversationHistory.push({ role: 'assistant', content: data.response });
      } else {
        appendAssistantMessage(data.response || 'Sorry, I encountered an issue processing that request.');
      }
    } catch (err) {
      if (thinkingIndicator) thinkingIndicator.classList.add('hidden');
      appendAssistantMessage(`⚠️ Network or server error: ${err.message}`);
    }
  }

  // Handle Form Submit
  chatForm?.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = chatInput?.value.trim();
    if (text) {
      handleSendMessage(text);
    }
  });

  // Handle New Chat
  newChatBtn?.addEventListener('click', () => {
    conversationHistory = [];
    if (chatMessages) chatMessages.innerHTML = '';
    if (heroState) heroState.classList.remove('hidden');
    if (chatInput) {
      chatInput.value = '';
      chatInput.style.height = 'auto';
      chatInput.focus();
    }
    if (sendBtn) sendBtn.disabled = true;
    updateScrollButton();
  });
});
