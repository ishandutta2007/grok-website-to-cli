/**
 * Grok CLI Bridge - Content Script
 *
 * Runs inside grok.com pages. Handles DOM operations requested
 * by the background service worker (forwarded from the Python CLI).
 *
 * KEY INSIGHT: Grok shows an action buttons footer (Copy, Like, Dislike,
 * Share, etc.) ONLY after generation completes. The "Create share link"
 * button (aria-label) is used as the anchor to detect these footers.
 */

(function () {
  if (window.__grok_bridge_injected) {
    return;
  }
  window.__grok_bridge_injected = true;

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const { type } = message;

    if (type === 'ping_content') {
      sendResponse({ pong: true });
      return false;
    }

    const handlers = {
      send_prompt: () => handleSendPrompt(message.prompt),
      check_response_status: () => handleCheckResponseStatus(),
      extract_last_code_block: () => handleExtractLastCodeBlock(),
      extract_full_response: () => handleExtractFullResponse(),
      diagnose: () => handleDiagnose(),
    };

    const handler = handlers[type];
    if (!handler) return false;

    handler()
      .then((data) => sendResponse(data || {}))
      .catch((err) => sendResponse({ __error: true, error: err.message || String(err) }));

    return true; // Keep message channel open for async sendResponse
  });

  // ── Prompt Submission ────────────────────────────────────────────────────

  async function handleSendPrompt(prompt) {
    const selectors = [
      'textarea',
      'div[contenteditable="true"]',
      '[role="textbox"]',
    ];

    let inputElement = null;
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) {
        inputElement = el;
        break;
      }
    }

    if (!inputElement) {
      throw new Error(
        'Could not find the prompt input element. Make sure grok.com is fully loaded and you are logged in.'
      );
    }

    const tag = inputElement.tagName.toLowerCase();

    if (tag === 'textarea') {
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        'value'
      ).set;
      nativeSetter.call(inputElement, prompt);
      inputElement.dispatchEvent(new Event('input', { bubbles: true }));
      inputElement.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (inputElement.isContentEditable) {
      inputElement.focus();
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(inputElement);
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('insertText', false, prompt);
    } else {
      inputElement.value = prompt;
      inputElement.dispatchEvent(new Event('input', { bubbles: true }));
      inputElement.dispatchEvent(new Event('change', { bubbles: true }));
    }

    await sleep(500);

    // Submit by pressing Enter
    const enterEventInit = {
      key: 'Enter',
      code: 'Enter',
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
    };
    inputElement.dispatchEvent(new KeyboardEvent('keydown', enterEventInit));
    inputElement.dispatchEvent(new KeyboardEvent('keypress', enterEventInit));
    inputElement.dispatchEvent(new KeyboardEvent('keyup', enterEventInit));

    // Also try submitting via a nearby submit button as fallback
    await sleep(300);
    const submitBtn = document.querySelector(
      'button[type="submit"], button[aria-label*="Send" i], button[data-testid*="send" i]'
    );
    if (submitBtn && isVisible(submitBtn)) {
      submitBtn.click();
    }

    return { submitted: true };
  }

  // ── Action Footer Detection ──────────────────────────────────────────────
  //
  // Grok shows an action footer after each completed AI response containing
  // buttons like Copy, Like, Dislike, Share ("Create share link").
  // The "Create share link" button has a stable aria-label we can rely on.

  /**
   * Find all action button footers (one per completed AI response).
   * Uses the "Create share link" button as the anchor.
   */
  function findActionFooters() {
    const shareButtons = document.querySelectorAll(
      '[aria-label="Create share link"], button[aria-label="Create share link"]'
    );
    const footers = [];
    for (const btn of shareButtons) {
      // Walk up to the footer container
      const footer = btn.closest('div') || btn.parentElement;
      if (footer) {
        footers.push(footer);
      }
    }
    return footers;
  }

  // ── Response Status Check ────────────────────────────────────────────────

  async function handleCheckResponseStatus() {
    const footers = findActionFooters();

    // Count code blocks
    const codeBlocks = document.querySelectorAll('pre code, pre, div.code-block code');
    const visibleCodeBlocks = Array.from(codeBlocks).filter(isVisible);

    // Diagnostic info
    const diag = {
      actionFooters: footers.length,
      shareBtn: !!document.querySelector('[aria-label="Create share link"]'),
      textareaFound: !!document.querySelector('textarea'),
    };

    return {
      codeBlockCount: visibleCodeBlocks.length,
      diag,
    };
  }

  // ── Code Block Extraction ────────────────────────────────────────────────

  async function handleExtractLastCodeBlock() {
    const codeBlocks = Array.from(
      document.querySelectorAll('pre code, pre, code.hljs, div.code-block code')
    ).filter(isVisible);

    if (codeBlocks.length === 0) {
      return { text: null, error: 'No code blocks found' };
    }

    const lastBlock = codeBlocks[codeBlocks.length - 1];

    // Strategy 1: Try to find and click a copy button near the code block
    const copiedText = await tryCopyButton(lastBlock);
    if (copiedText) {
      return { text: copiedText, method: 'copy_button' };
    }

    // Strategy 2: Read text content directly
    const text = lastBlock.innerText || lastBlock.textContent;
    if (text && text.trim()) {
      return { text: text.trim(), method: 'innerText' };
    }

    return { text: null, error: 'Code block found but empty' };
  }

  async function tryCopyButton(codeBlockElement) {
    let container = codeBlockElement;
    for (let i = 0; i < 6; i++) {
      container = container.parentElement;
      if (!container || container === document.body) break;

      const btn = container.querySelector(
        'button[aria-label*="Copy" i], button[title*="Copy" i], button.copy-button, button[class*="copy" i]'
      );

      if (btn && isVisible(btn)) {
        try {
          btn.click();
          await sleep(300);
          const text = await navigator.clipboard.readText();
          if (text && text.trim()) {
            return text.trim();
          }
        } catch (e) {
          console.warn('[Grok CLI Bridge] Clipboard read failed:', e);
        }
      }
    }
    return null;
  }

  // ── Full Response Extraction ─────────────────────────────────────────────

  /**
   * Find the Copy button in the action footer for the last response.
   * The Copy button is typically the first button in the footer.
   */
  function findCopyButtonInFooter() {
    const footers = findActionFooters();
    if (footers.length === 0) return null;

    const lastFooter = footers[footers.length - 1];

    // Look for a copy button in the footer
    const copyBtn = lastFooter.querySelector(
      'button[aria-label*="Copy" i], [aria-label*="Copy" i], button[title*="Copy" i]'
    );
    if (copyBtn) return copyBtn;

    // Fallback: first button in the footer (usually Copy)
    const firstBtn = lastFooter.querySelector('button, [role="button"]');
    return firstBtn || null;
  }

  async function handleExtractFullResponse() {
    // ── Strategy 1: Click the Copy button in the action footer ──────
    // This preserves the original markdown formatting.
    const copyBtn = findCopyButtonInFooter();
    if (copyBtn) {
      try {
        copyBtn.click();
        await sleep(400);
        const copiedText = await navigator.clipboard.readText();
        if (copiedText && copiedText.trim()) {
          return { text: copiedText.trim(), method: 'footer_copy_button' };
        }
      } catch (e) {
        console.warn('[Grok CLI Bridge] Footer copy button failed:', e);
      }
    }

    // ── Strategy 2: Fallback to innerText extraction ────────────────
    const selectors = [
      'div[data-testid="message-content"]',
      'div.message-content',
      'div.markdown',
      'div[class*="response"]',
    ];

    for (const sel of selectors) {
      const elements = Array.from(document.querySelectorAll(sel)).filter(isVisible);
      if (elements.length > 0) {
        const lastEl = elements[elements.length - 1];
        const text = lastEl.innerText || lastEl.textContent;
        if (text && text.trim()) {
          return { text: text.trim(), method: 'innertext_fallback' };
        }
      }
    }

    return { text: null, error: 'No response containers found' };
  }

  // ── DOM Diagnostics ──────────────────────────────────────────────────────

  async function handleDiagnose() {
    const selectorTests = {
      'textarea': document.querySelector('textarea')?.tagName ?? null,
      'actionFooters (Create share link)': findActionFooters().length,
      '[aria-label="Create share link"]': !!document.querySelector('[aria-label="Create share link"]'),
      'button[aria-label*="Stop" i]': !!document.querySelector('button[aria-label*="Stop" i]'),
      '.animate-spin': !!document.querySelector('.animate-spin'),
      'pre code': document.querySelectorAll('pre code').length,
      'div[data-testid="message-content"]': document.querySelectorAll('div[data-testid="message-content"]').length,
      'div.message-content': document.querySelectorAll('div.message-content').length,
      'div.markdown': document.querySelectorAll('div.markdown').length,
    };

    return {
      url: window.location.href,
      actionFooterCount: findActionFooters().length,
      selectorTests,
    };
  }

  // ── Utilities ────────────────────────────────────────────────────────────

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return (
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0' &&
      el.offsetParent !== null
    );
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
})();
