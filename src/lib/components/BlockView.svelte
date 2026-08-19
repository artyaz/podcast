<script lang="ts">
	import { isActive, playFrom, playback, stopPlayback } from '$lib/audio.svelte';
	import {
		askAt,
		lesson,
		mergeBlockWithPrevious,
		removeBlock,
		splitBlockAt,
		type Block
	} from '$lib/lesson.svelte';
	import { hasKey } from '$lib/settings.svelte';
	import { startRecording as startMic, stopRecording as stopMic, transcribeBlob } from '$lib/voice';

	interface BlockViewProps {
		block: Block;
		onRequestActions: (block: Block) => void;
		onFocused: (blockId: string) => void;
	}

	let { block, onRequestActions, onFocused }: BlockViewProps = $props();

	/**
	 * Where a question typed into this block should be inserted: after whatever
	 * precedes it, so the answer lands exactly where the reader was reading.
	 * Derived from the live lesson so a reordered document still anchors right.
	 */
	const previousBlockId = $derived.by(() => {
		const index = lesson.blocks.findIndex((candidate) => candidate.id === block.id);
		return index > 0 ? lesson.blocks[index - 1].id : null;
	});

	/**
	 * The text this block held when the reader last focused it. Anything typed
	 * after that makes the block dirty, which is what reveals the submit button —
	 * the reader types their question straight into the lesson, exactly where they
	 * want the answer to land.
	 */
	let textAtFocus = $state('');
	let isDirty = $state(false);
	let hasFocus = $state(false);
	let recording = $state(false);
	let transcribing = $state(false);
	let voiceError = $state('');

	let longPressTimer: ReturnType<typeof setTimeout> | null = null;

	function beginLongPress() {
		cancelLongPress();
		longPressTimer = setTimeout(() => {
			longPressTimer = null;
			onRequestActions(block);
		}, 480);
	}

	function cancelLongPress() {
		if (longPressTimer) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
		}
	}

	function handleInput() {
		// `bind:innerText` has already written the text into the block, so this only
		// judges whether the reader has changed it since focusing.
		isDirty = block.text.trim() !== textAtFocus.trim();
		if (isDirty && block.origin === 'lesson') block.origin = 'user';
	}

	function handleFocus() {
		hasFocus = true;
		textAtFocus = block.text;
		onFocused(block.id);
	}

	function handleBlur() {
		hasFocus = false;
		// The submit button lives outside the block, so it must survive the blur
		// that clicking it causes. Dirtiness is cleared on submit, not on blur.
	}

	async function askFromHere(questionText: string, replaceCurrent: boolean) {
		const trimmed = questionText.trim();
		if (!trimmed) return;
		isDirty = false;
		voiceError = '';
		const anchorBlockId = replaceCurrent ? previousBlockId : block.id;
		if (replaceCurrent) removeBlock(block.id);
		await askAt(anchorBlockId, trimmed);
	}

	async function submitQuestion() {
		await askFromHere(block.text, true);
	}

	async function toggleMic() {
		voiceError = '';
		if (recording) {
			recording = false;
			transcribing = true;
			try {
				const blob = await stopMic();
				const spoken = await transcribeBlob(blob);
				await askFromHere(spoken, isDirty);
			} catch (failure) {
				voiceError = (failure as Error).message;
			} finally {
				transcribing = false;
			}
			return;
		}
		try {
			await startMic();
			recording = true;
		} catch (failure) {
			voiceError = `Microphone unavailable: ${(failure as Error).message}`;
		}
	}

	function caretOffset(element: HTMLElement): number {
		const selection = window.getSelection();
		if (!selection || selection.rangeCount === 0) return 0;
		const range = selection.getRangeAt(0);
		const prefix = range.cloneRange();
		prefix.selectNodeContents(element);
		prefix.setEnd(range.endContainer, range.endOffset);
		return prefix.toString().length;
	}

	function focusBlock(blockId: string, offset: number) {
		const element = document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
		if (!element) return;
		element.focus();
		const selection = window.getSelection();
		if (!selection) return;
		const textNode = element.firstChild;
		const range = document.createRange();
		if (textNode && textNode.nodeType === Node.TEXT_NODE) {
			const position = Math.max(0, Math.min(offset, textNode.textContent?.length || 0));
			range.setStart(textNode, position);
			range.collapse(true);
		} else {
			range.selectNodeContents(element);
			range.collapse(offset === 0);
		}
		selection.removeAllRanges();
		selection.addRange(range);
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
			event.preventDefault();
			submitQuestion();
			return;
		}
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			const element = event.currentTarget as HTMLElement;
			const createdId = splitBlockAt(block.id, caretOffset(element));
			if (createdId) queueMicrotask(() => focusBlock(createdId, 0));
			return;
		}
		if (event.key === 'Backspace') {
			const element = event.currentTarget as HTMLElement;
			if (caretOffset(element) !== 0) return;
			const previousId = mergeBlockWithPrevious(block.id);
			if (!previousId) return;
			event.preventDefault();
			const previous = lesson.blocks.find((candidate) => candidate.id === previousId);
			const caret = Math.max(0, (previous?.text.length || 0) - block.text.length);
			queueMicrotask(() => focusBlock(previousId, caret));
		}
	}

	function togglePlayback() {
		if (isActive(block.id) && playback.playing) stopPlayback();
		else playFrom(block.id);
	}

	const statusLabel: Record<string, string> = {
		verified: 'verified',
		contested: 'contested',
		unverified: 'unverified',
		inferred: 'reasoned'
	};
</script>

<div
	class="block"
	role="group"
	class:heading={block.kind === 'heading'}
	class:aside={block.kind === 'aside'}
	class:gap={block.kind === 'gap'}
	class:question={block.origin === 'question'}
	class:sounding={isActive(block.id)}
	onpointerdown={beginLongPress}
	onpointerup={cancelLongPress}
	onpointermove={cancelLongPress}
	onpointercancel={cancelLongPress}
	oncontextmenu={(event) => {
		event.preventDefault();
		onRequestActions(block);
	}}
>
	{#if block.pending}
		<p class="text shimmering">{block.text}</p>
	{:else}
		<!-- A div rather than a p: role="textbox" on a paragraph is a noninteractive
		     element given an interactive role, which assistive technology reads
		     inconsistently. The styling is on .text, so both branches match. -->
		<div
			class="text"
			contenteditable="true"
			data-block-id={block.id}
			bind:innerText={block.text}
			oninput={handleInput}
			onfocus={handleFocus}
			onblur={handleBlur}
			onkeydown={handleKeydown}
			role="textbox"
			tabindex="0"
			aria-multiline="true"
			aria-label="Lesson block — type a question here to ask about it"
		></div>
	{/if}

	{#if !block.pending}
		<button
			class="play"
			class:visible={isActive(block.id)}
			onclick={togglePlayback}
			aria-label={isActive(block.id) && playback.playing
				? 'Stop'
				: 'Play from here'}
			title="Play from here"
		>
			{#if isActive(block.id) && playback.synthesizing}
				<span class="spinner"></span>
			{:else if isActive(block.id) && playback.playing}
				◼
			{:else}
				▶
			{/if}
		</button>
	{/if}

	{#if block.sources.length || block.origin === 'question'}
		<div class="meta">
			{#if block.origin === 'question'}
				<span class="chip asked">you asked</span>
			{:else}
				<span class="chip status {block.status}">{statusLabel[block.status] ?? block.status}</span>
			{/if}
			{#each block.sources.slice(0, 4) as source (source.url)}
				<a class="chip source" href={source.url} target="_blank" rel="noreferrer noopener">
					{new URL(source.url).hostname.replace(/^www\./, '')}
				</a>
			{/each}
		</div>
	{/if}
</div>

{#if !block.pending && (isDirty || hasFocus || recording || transcribing || voiceError)}
	<div class="submitbar">
		{#if isDirty}
			<button class="submit" onclick={submitQuestion}>Ask this →</button>
			<span class="hint">⌘↵</span>
		{/if}
		<button
			class="mic"
			class:recording
			onmousedown={(event) => event.preventDefault()}
			onclick={toggleMic}
			disabled={transcribing || !hasKey('openrouter')}
			aria-label={recording ? 'Stop recording' : 'Ask by voice'}
			title={hasKey('openrouter') ? 'Ask by voice' : 'Add an OpenRouter key in Settings'}
		>
			{#if transcribing}
				<span class="spinner"></span>
			{:else if recording}
				◼
			{:else}
				●
			{/if}
		</button>
		{#if voiceError}
			<span class="voiceerr">{voiceError}</span>
		{/if}
	</div>
{/if}

<style>
	.block {
		position: relative;
		padding: 2px 44px 2px 0;
	}
	.text {
		margin: 0 0 18px;
		font-family: var(--serif);
		font-size: 19px;
		line-height: 1.62;
		outline: none;
		white-space: pre-wrap;
	}
	.text:focus {
		/* A caret is enough of an affordance; a box would fight the reading. */
		background: color-mix(in srgb, var(--ink) 3%, transparent);
		border-radius: 6px;
	}
	.heading .text {
		font-size: 15px;
		font-family: var(--sans);
		text-transform: uppercase;
		letter-spacing: 0.07em;
		color: var(--muted);
		margin-top: 26px;
		margin-bottom: 12px;
	}
	.aside .text {
		font-size: 17px;
		color: var(--muted);
		border-left: 2px solid var(--line);
		padding-left: 14px;
	}
	.gap .text {
		font-family: var(--sans);
		font-size: 16px;
		color: var(--warn);
	}
	.question .text {
		font-family: var(--sans);
		font-size: 17px;
		font-weight: 600;
		border-left: 2px solid var(--ink);
		padding-left: 14px;
	}
	.sounding .text {
		background: color-mix(in srgb, var(--ok) 10%, transparent);
		border-radius: 6px;
	}

	.play {
		position: absolute;
		top: 0;
		right: 0;
		width: 34px;
		height: 34px;
		border-radius: 50%;
		border: 1.5px solid var(--line);
		background: var(--panel);
		color: var(--muted);
		font-size: 12px;
		line-height: 1;
		opacity: 0;
		transition: opacity 0.14s ease;
	}
	.block:hover .play,
	.play:focus-visible,
	.play.visible {
		opacity: 1;
	}
	.play.visible {
		border-color: var(--ok);
		color: var(--ok);
	}
	/* Touch has no hover, so the control is always reachable there. */
	@media (hover: none) {
		.play {
			opacity: 0.55;
		}
	}

	.spinner {
		display: inline-block;
		width: 10px;
		height: 10px;
		border: 1.5px solid var(--line);
		border-top-color: var(--ok);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.meta {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin: -10px 0 20px;
	}
	.chip {
		font-family: var(--sans);
		font-size: 11px;
		letter-spacing: 0.03em;
		padding: 3px 8px;
		border-radius: 999px;
		border: 1px solid var(--line);
		color: var(--muted);
		text-decoration: none;
	}
	.chip.status.verified {
		color: var(--ok);
		border-color: color-mix(in srgb, var(--ok) 40%, var(--line));
	}
	.chip.status.contested {
		color: var(--warn);
		border-color: color-mix(in srgb, var(--warn) 40%, var(--line));
	}
	.chip.status.unverified {
		color: var(--bad);
		border-color: color-mix(in srgb, var(--bad) 35%, var(--line));
	}
	.chip.asked {
		color: var(--ink);
		border-color: var(--ink);
	}

	.submitbar {
		display: flex;
		align-items: center;
		gap: 10px;
		margin: -8px 0 22px;
	}
	.submit {
		padding: 9px 16px;
		border-radius: 999px;
		background: var(--ink);
		color: var(--bg);
		font-size: 14px;
		font-weight: 600;
	}
	.hint {
		font-size: 12px;
		color: var(--muted);
	}
	.mic {
		width: 34px;
		height: 34px;
		border-radius: 50%;
		border: 1.5px solid var(--line);
		background: var(--panel);
		color: var(--muted);
		font-size: 12px;
		line-height: 1;
	}
	.mic.recording {
		border-color: var(--bad);
		color: var(--bad);
	}
	.mic:disabled {
		opacity: 0.4;
	}
	.voiceerr {
		font-size: 12px;
		color: var(--bad);
		line-height: 1.4;
	}
</style>
