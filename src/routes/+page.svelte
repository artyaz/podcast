<script lang="ts">
	import AskBar from '$lib/components/AskBar.svelte';
	import BlockActions from '$lib/components/BlockActions.svelte';
	import BlockView from '$lib/components/BlockView.svelte';
	import ReasoningLog from '$lib/components/ReasoningLog.svelte';
	import SubtopicModal from '$lib/components/SubtopicModal.svelte';
	import { playFrom, playback, stopPlayback } from '$lib/audio.svelte';
	import {
		activeLesson,
		backToLibrary,
		createLesson,
		library,
		openLesson
	} from '$lib/library.svelte';
	import { lesson, runResearch, type Block, type WorkBeat } from '$lib/lesson.svelte';
	import { canResearch, settings } from '$lib/settings.svelte';

	let topicDraft = $state('');
	let showSubtopicModal = $state(false);
	let sheetBlock = $state<Block | null>(null);
	let anchorBlockId = $state<string | null>(null);
	let expandedWork = $state<WorkBeat[] | null>(null);

	const current = $derived(activeLesson());
	const firstBlockId = $derived(lesson.blocks[0]?.id ?? null);
	const stillWriting = $derived(Boolean(current?.running || lesson.running));

	function startResearch(subtopics: { title: string; angle?: string }[] = []) {
		const topic = topicDraft.trim();
		if (!topic) return;
		showSubtopicModal = false;
		createLesson(topic);
		runResearch(topic, subtopics);
		topicDraft = '';
	}

	function begin() {
		startResearch();
	}

	function beginWithSegments(subtopics: { title: string; angle?: string }[]) {
		startResearch(subtopics);
	}

	function itemStatus(record: {
		running: boolean;
		finished: boolean;
		errorMessage: string;
		blocks: { id: string }[];
	}) {
		if (record.errorMessage) return 'stopped';
		if (record.running && record.blocks.length) return 'writing';
		if (record.running) return 'researching';
		if (record.finished || record.blocks.length) return 'ready';
		return 'draft';
	}

	function itemInfo(record: { blocks: { id: string }[]; running: boolean }) {
		if (record.running && !record.blocks.length) return 'in progress';
		if (record.blocks.length) return `${record.blocks.length} blocks`;
		return '';
	}

	function workFor(record: { id: string; work?: WorkBeat[] }) {
		if (record.id === library.activeId && lesson.work.length) return lesson.work;
		return record.work || [];
	}
</script>

<svelte:head>
	<title>
		{library.view === 'library' ? 'Praxis' : lesson.topic || 'Praxis'}
	</title>
</svelte:head>

{#if library.view === 'library'}
	<div class="library">
		<div class="opening">
			<h1>What should this episode be about?</h1>
			<p class="lede">
				Name a question worth arguing about. It gets scoped, researched against primary sources,
				audited for what is missing, and written to be listened to.
			</p>

			<textarea
				bind:value={topicDraft}
				rows="3"
				placeholder="Whether rent control reduces housing supply — and what the evidence actually shows"
				onkeydown={(event) => {
					if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) begin();
				}}
			></textarea>

			{#if canResearch()}
				<div class="startrow">
					<button class="primary" onclick={begin} disabled={!topicDraft.trim()}>
						Research and write it
					</button>
					<button
						class="ghost"
						onclick={() => (showSubtopicModal = true)}
						disabled={!topicDraft.trim()}
						title="Split the subject into evenly divided segments first"
					>
						Break into segments
					</button>
				</div>
				<p class="lede small">
					Breaking it up first divides the subject into segments, spreads the research evenly across
					them, and makes the episode follow them in order. Useful when a subject is broad enough that
					one pass would skim it.
				</p>
			{:else}
				<p class="warn">
					Add an OpenRouter key and an Exa key in <a href="/settings">Settings</a> first. Nothing is
					shipped with the app — every key lives in this browser only.
				</p>
			{/if}
		</div>

		{#if library.lessons.length}
			<ul class="entries">
				{#each library.lessons as record (record.id)}
					<li>
						<button class="entry" onclick={() => openLesson(record.id)}>
							<h2>{record.topic}</h2>
							<p class="meta">
								<span>{itemStatus(record)}</span>
								{#if itemInfo(record)}
									<span>{itemInfo(record)}</span>
								{/if}
							</p>
						</button>
						{#if record.running || (record.id === library.activeId && lesson.running)}
							<ReasoningLog
								beats={workFor(record)}
								running={true}
								cap={3}
								onExpand={() => (expandedWork = workFor(record))}
							/>
						{/if}
						{#if record.errorMessage}
							<p class="itemerror">{record.errorMessage}</p>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>

	{#if showSubtopicModal}
		<SubtopicModal
			topic={topicDraft.trim()}
			onClose={() => (showSubtopicModal = false)}
			onProceed={beginWithSegments}
		/>
	{/if}
{:else}
	<div class="lesson" class:hasbar={lesson.blocks.length > 0}>
		<button class="back" onclick={backToLibrary}>← Library</button>
		<h1 class="topic">{lesson.topic}</h1>

		{#if stillWriting}
			<p class="working shimmering">Still writing this episode</p>
		{/if}

		{#if lesson.errorMessage}
			<p class="itemerror">{lesson.errorMessage}</p>
		{/if}

		{#if lesson.blocks.length}
			<div class="transport">
				{#if playback.playing}
					<button class="ghost" onclick={stopPlayback}>◼ Stop</button>
				{:else if firstBlockId}
					<button class="ghost" onclick={() => playFrom(firstBlockId)}>▶ Play episode</button>
				{/if}
				<span class="voicelabel">
					{settings.speechProvider === 'speechify'
						? settings.speechifyVoice
						: settings.kokoroVoice}
				</span>
				{#if playback.errorMessage}
					<span class="playerror">{playback.errorMessage}</span>
				{/if}
			</div>
		{/if}

		{#each lesson.blocks as block (block.id)}
			<BlockView
				{block}
				onRequestActions={(target) => (sheetBlock = target)}
				onFocused={(blockId) => (anchorBlockId = blockId)}
			/>
		{/each}

		{#if stillWriting}
			<p class="working shimmering">Still writing this episode</p>
		{:else if lesson.finished && lesson.blocks.length}
			<p class="footnote">
				Ask anything below, or put the cursor into any paragraph and type — your question becomes
				part of the episode, and the answer lands right under it. Enter starts a new paragraph.
			</p>
		{/if}
	</div>

	{#if lesson.blocks.length}
		<AskBar {anchorBlockId} />
	{/if}
	<BlockActions block={sheetBlock} onClose={() => (sheetBlock = null)} />
{/if}

{#if expandedWork}
	<div
		class="sheet"
		role="dialog"
		aria-modal="true"
		aria-label="Reasoning"
		tabindex="-1"
		onclick={(event) => {
			if (event.currentTarget === event.target) expandedWork = null;
		}}
		onkeydown={(event) => {
			if (event.key === 'Escape') expandedWork = null;
		}}
	>
		<div class="sheetpanel">
			<div class="sheethead">
				<h2>Reasoning</h2>
				<button class="ghost tiny" onclick={() => (expandedWork = null)}>Close</button>
			</div>
			<ReasoningLog beats={expandedWork} running={stillWriting} />
		</div>
	</div>
{/if}

<style>
	.library,
	.lesson {
		padding-bottom: 40px;
	}
	.lesson.hasbar {
		padding-bottom: 96px;
	}
	.opening {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 12px 0 28px;
	}
	h1 {
		font-family: var(--serif);
		font-size: 28px;
		line-height: 1.25;
		margin: 0;
	}
	.topic {
		font-size: 25px;
		margin: 0 0 14px;
	}
	.lede {
		margin: 0;
		font-size: 15px;
		line-height: 1.6;
		color: var(--muted);
	}
	textarea {
		padding: 14px 16px;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		background: var(--panel);
		color: var(--ink);
		outline: none;
		resize: vertical;
		line-height: 1.55;
		font-family: var(--serif);
	}
	textarea:focus {
		border-color: color-mix(in srgb, var(--ink) 45%, var(--line));
	}
	.startrow {
		display: flex;
		gap: 9px;
		flex-wrap: wrap;
	}
	.primary {
		padding: 14px 24px;
		border-radius: 13px;
		background: var(--ink);
		color: var(--bg);
		font-weight: 600;
	}
	.primary:disabled {
		opacity: 0.4;
	}
	.lede.small {
		font-size: 13px;
		margin-top: -6px;
	}
	.warn {
		margin: 0;
		font-size: 13px;
		line-height: 1.55;
		color: var(--muted);
		border: 1px solid var(--line);
		border-radius: 12px;
		padding: 12px 14px;
	}

	.entries {
		list-style: none;
		margin: 8px 0 0;
		padding: 0;
	}
	.entries > li {
		padding: 18px 0 20px;
		border-top: 1px solid var(--rule);
	}
	.entry {
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: 100%;
		text-align: left;
		padding: 0;
		background: none;
		border: none;
	}
	.entry h2 {
		font-family: var(--serif);
		font-size: 22px;
		line-height: 1.3;
		margin: 0;
		font-weight: 600;
	}
	.meta {
		display: flex;
		gap: 12px;
		margin: 0;
		font-size: 12px;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}
	.itemerror {
		margin: 8px 0 0;
		font-size: 13px;
		color: var(--bad);
		line-height: 1.5;
	}

	.back {
		font-size: 13px;
		color: var(--muted);
		margin: 0 0 12px;
		padding: 0;
	}
	.working {
		margin: 0 0 18px;
		font-size: 13px;
		letter-spacing: 0.04em;
	}
	.transport {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-bottom: 22px;
		flex-wrap: wrap;
	}
	.ghost {
		padding: 9px 15px;
		border-radius: 999px;
		border: 1.5px solid var(--line);
		font-size: 14px;
	}
	.ghost.tiny {
		padding: 3px 9px;
		font-size: 11px;
		border-radius: 999px;
	}
	.voicelabel {
		font-size: 12px;
		color: var(--muted);
	}
	.playerror {
		font-size: 12px;
		color: var(--bad);
	}
	.footnote {
		margin: 26px 0 0;
		font-size: 13px;
		line-height: 1.55;
		color: var(--muted);
		border-top: 1px solid var(--rule);
		padding-top: 14px;
	}

	.sheet {
		position: fixed;
		inset: 0;
		z-index: 40;
		background: color-mix(in srgb, var(--bg) 72%, transparent);
		display: flex;
		align-items: flex-end;
		justify-content: center;
		padding: 16px 16px calc(16px + var(--safe-bottom));
	}
	.sheetpanel {
		width: min(34rem, 100%);
		max-height: min(78vh, 640px);
		overflow: auto;
		background: var(--bg);
		border-top: 1px solid var(--rule);
		padding: 16px 4px 24px;
	}
	.sheethead {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		margin-bottom: 12px;
	}
	.sheethead h2 {
		margin: 0;
		font-family: var(--serif);
		font-size: 22px;
	}
</style>
