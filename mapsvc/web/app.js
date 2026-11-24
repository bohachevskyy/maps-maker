(() => {
  "use strict"

  const STORAGE_KEY = "mapsvc.promptHistory"
  const form = document.querySelector("#prompt-form")
  const promptBox = document.querySelector("#prompt-box")
  const sendButton = document.querySelector("#send-button")
  const cancelButton = document.querySelector("#cancel-button")
  const turns = document.querySelector("#turns")
  const emptyState = document.querySelector("#empty-state")
  const historyNode = document.querySelector("#history")
  const sidebar = document.querySelector("#sidebar")
  const sidebarOpen = document.querySelector("#sidebar-open")
  const sidebarClose = document.querySelector("#sidebar-close")
  const sidebarScrim = document.querySelector("#sidebar-scrim")
  let controller = null
  let history = readHistory()

  renderHistory()
  resizePrompt()
  updateSendButton()

  form.addEventListener("submit", (event) => {
    event.preventDefault()
    submitPrompt(promptBox.value)
  })

  promptBox.addEventListener("input", () => {
    resizePrompt()
    updateSendButton()
  })

  promptBox.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault()
      form.requestSubmit()
    }
  })

  document.querySelector("#examples").addEventListener("click", (event) => {
    const button = event.target.closest("button")
    if (button) submitPrompt(button.textContent.trim())
  })

  document.querySelector("#new-map").addEventListener("click", () => {
    if (controller) controller.abort()
    turns.replaceChildren()
    emptyState.hidden = false
    promptBox.value = ""
    resizePrompt()
    updateSendButton()
    closeSidebar()
    promptBox.focus()
  })

  cancelButton.addEventListener("click", () => controller?.abort())
  sidebarOpen.addEventListener("click", openSidebar)
  sidebarClose.addEventListener("click", closeSidebar)
  sidebarScrim.addEventListener("click", closeSidebar)
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar()
  })

  async function submitPrompt(value) {
    const prompt = value.trim()
    if (!prompt || controller) return

    emptyState.hidden = true
    addToHistory(prompt)
    const turn = document.createElement("article")
    turn.className = "turn"

    const userMessage = document.createElement("div")
    userMessage.className = "user-message"
    userMessage.textContent = prompt
    turn.append(userMessage)

    const waiting = document.querySelector("#waiting-template").content.cloneNode(true)
    turn.append(waiting)
    turns.append(turn)
    scrollToTurn(turn)

    promptBox.value = ""
    resizePrompt()
    setBusy(true)
    controller = new AbortController()
    const activeController = controller
    const started = Date.now()
    const elapsedNode = turn.querySelector(".elapsed")
    const hintNode = turn.querySelector(".waiting-hint")
    const timer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000)
      elapsedNode.textContent = `${seconds}s`
      hintNode.textContent = seconds < 12 ? "finding data…" : seconds < 30 ? "checking sources…" : "drawing…"
    }, 1000)

    try {
      const response = await fetch(apiUrl("/describe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
        signal: activeController.signal
      })

      if (!response.ok) throw await serviceError(response)
      const result = await response.json()
      replaceWaiting(turn, createResponseCard(result))
    } catch (error) {
      if (error.name === "AbortError") {
        replaceWaiting(turn, createCancelledCard())
      } else {
        replaceWaiting(turn, createErrorCard(error))
      }
    } finally {
      window.clearInterval(timer)
      if (controller === activeController) {
        controller = null
        setBusy(false)
      }
    }
  }

  function createResponseCard(result) {
    const card = document.createElement("section")
    card.className = "response-card"
    card.setAttribute("aria-label", "Generated map")

    const mapFrame = document.createElement("div")
    mapFrame.className = "map-frame"
    setSvg(mapFrame, result.svg)

    const body = document.createElement("div")
    body.className = "response-body"

    const reasoning = document.createElement("p")
    reasoning.className = "reasoning"
    reasoning.textContent = result.reasoning || "Rendered from the generated manifest."

    const actions = document.createElement("div")
    actions.className = "response-actions"

    const download = actionButton("Download SVG")
    download.addEventListener("click", () => downloadSvg(mapFrame.innerHTML, result.manifest))

    const copy = actionButton("Copy manifest")
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(JSON.stringify(result.manifest, null, 2))
        temporaryLabel(copy, "Copied")
      } catch (_) {
        temporaryLabel(copy, "Copy failed")
      }
    })

    const details = document.createElement("details")
    details.className = "manifest-details"
    const summary = document.createElement("summary")
    summary.textContent = "How it was made"
    const editor = document.createElement("textarea")
    editor.className = "manifest-editor"
    editor.setAttribute("aria-label", "Editable manifest JSON")
    editor.spellcheck = false
    editor.value = JSON.stringify(result.manifest, null, 2)
    const footer = document.createElement("div")
    footer.className = "manifest-footer"
    const status = document.createElement("span")
    status.className = "render-status"
    status.setAttribute("role", "status")
    const rerender = document.createElement("button")
    rerender.type = "button"
    rerender.className = "rerender-button"
    rerender.textContent = "Re-render"
    rerender.addEventListener("click", () => rerenderManifest(editor, mapFrame, status, rerender, result))
    footer.append(status, rerender)
    details.append(summary, editor, footer)

    actions.append(download, copy, details)
    body.append(reasoning, actions)
    card.append(mapFrame, body)
    return card
  }

  async function rerenderManifest(editor, mapFrame, status, button, result) {
    let manifest
    status.classList.remove("error")
    try {
      manifest = JSON.parse(editor.value)
    } catch (error) {
      status.textContent = `Invalid JSON: ${error.message}`
      status.classList.add("error")
      return
    }

    button.disabled = true
    status.textContent = "Rendering…"
    try {
      const response = await fetch(apiUrl("/map"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(manifest)
      })
      if (!response.ok) throw await serviceError(response)
      setSvg(mapFrame, await response.text())
      result.manifest = manifest
      status.textContent = "Map updated"
    } catch (error) {
      status.textContent = friendlyMessage(error)
      status.classList.add("error")
    } finally {
      button.disabled = false
    }
  }

  function setSvg(frame, svgText) {
    const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml")
    if (parsed.querySelector("parsererror") || parsed.documentElement.localName !== "svg") {
      throw new Error("The service returned an invalid SVG.")
    }
    frame.replaceChildren(document.importNode(parsed.documentElement, true))
  }

  function createErrorCard(error) {
    const card = document.createElement("section")
    card.className = "response-card error-card"
    card.setAttribute("role", "alert")
    const title = document.createElement("p")
    title.className = "error-title"
    title.textContent = error.status === 422 ? "This map could not be made" : "Something went wrong"
    const message = document.createElement("p")
    message.className = "error-message"
    message.textContent = friendlyMessage(error)
    card.append(title, message)
    if (error.field) {
      const field = document.createElement("div")
      field.className = "error-field"
      field.textContent = `Check: ${error.field}`
      card.append(field)
    }
    return card
  }

  function createCancelledCard() {
    const card = document.createElement("section")
    card.className = "response-card error-card cancelled-card"
    card.setAttribute("role", "status")
    const title = document.createElement("p")
    title.className = "error-title"
    title.textContent = "Map creation cancelled"
    const message = document.createElement("p")
    message.className = "error-message"
    message.textContent = "You can edit the prompt and try again."
    card.append(title, message)
    return card
  }

  function apiUrl(path) {
    return (window.MAPSVC_API_BASE || "").replace(/\/+$/, "") + path
  }

  async function serviceError(response) {
    let payload = {}
    try { payload = await response.json() } catch (_) { /* response was not JSON */ }
    const error = new Error(payload.error || `The service returned ${response.status}.`)
    error.status = response.status
    error.field = payload.field || ""
    error.retryAfter = response.headers.get("Retry-After")
    return error
  }

  function friendlyMessage(error) {
    if (error.status === 503) {
      const retry = error.retryAfter ? ` in ${error.retryAfter} seconds` : ""
      return `A data source is temporarily unavailable — try again${retry}.`
    }
    if (error.status === 422 || error.status === 429) return error.message
    if (error instanceof TypeError) return "Could not reach the map service. Check your connection and try again."
    return error.message || "An unexpected error occurred."
  }

  function actionButton(label) {
    const button = document.createElement("button")
    button.type = "button"
    button.className = "action-button"
    button.textContent = label
    return button
  }

  function downloadSvg(svg, manifest) {
    const blob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" })
    const link = document.createElement("a")
    link.href = URL.createObjectURL(blob)
    const name = manifest?.variable?.id || manifest?.level || "map"
    link.download = `${String(name).replace(/[^a-z0-9_-]+/gi, "-")}.svg`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(link.href), 1000)
  }

  function replaceWaiting(turn, node) {
    turn.querySelector(".waiting-card")?.replaceWith(node)
    scrollToTurn(turn)
  }

  function setBusy(busy) {
    promptBox.disabled = busy
    cancelButton.hidden = !busy
    sendButton.hidden = busy
    updateSendButton()
  }

  function updateSendButton() {
    sendButton.disabled = Boolean(controller) || !promptBox.value.trim()
  }

  function resizePrompt() {
    promptBox.style.height = "auto"
    promptBox.style.height = `${Math.min(promptBox.scrollHeight, 180)}px`
  }

  function scrollToTurn(turn) {
    window.requestAnimationFrame(() => turn.scrollIntoView({ behavior: "smooth", block: "start" }))
  }

  function temporaryLabel(button, label) {
    const original = button.textContent
    button.textContent = label
    window.setTimeout(() => { button.textContent = original }, 1400)
  }

  function readHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]")
      return Array.isArray(parsed) ? parsed.filter(item => typeof item === "string").slice(0, 20) : []
    } catch (_) {
      return []
    }
  }

  function addToHistory(prompt) {
    history = [prompt, ...history.filter(item => item !== prompt)].slice(0, 20)
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)) } catch (_) { /* storage may be disabled */ }
    renderHistory()
  }

  function renderHistory() {
    historyNode.replaceChildren()
    if (!history.length) {
      const empty = document.createElement("div")
      empty.className = "history-empty"
      empty.textContent = "Your prompts will appear here."
      historyNode.append(empty)
      return
    }
    history.forEach(prompt => {
      const button = document.createElement("button")
      button.type = "button"
      button.textContent = prompt
      button.title = prompt
      button.addEventListener("click", () => {
        promptBox.value = prompt
        resizePrompt()
        updateSendButton()
        closeSidebar()
        promptBox.focus()
      })
      historyNode.append(button)
    })
  }

  function openSidebar() {
    sidebar.classList.add("open")
    sidebarScrim.hidden = false
    sidebarOpen.setAttribute("aria-expanded", "true")
  }

  function closeSidebar() {
    sidebar.classList.remove("open")
    sidebarScrim.hidden = true
    sidebarOpen.setAttribute("aria-expanded", "false")
  }
})()
