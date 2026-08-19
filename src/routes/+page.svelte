<script lang="ts">
	import AskBar from '$lib/components/AskBar.svelte';
	import BlockActions from '$lib/components/BlockActions.svelte';
	import BlockView from '$lib/components/BlockView.svelte';
	import SubtopicModal from '$lib/components/SubtopicModal.svelte';
	import { playFrom, playback, stopPlayback } from '$lib/audio.svelte';
	import {
		activeLesson,
		backToLibrary,
		backToTopics,
		createLesson,
		library,
		openLesson,
		openSection,
		sectionBlocks
	} from '$lib/library.svelte';
	import { lesson, runResearch, type Block } from '$lib/lesson.svelte';
	import { canResearch, settings } from '$lib/settings.svelte';

	let topicDraft = $state('');
	let showSubtopicModal = $state(false);
	let sheetBlock = $state<Block | null>(null);
	let anchorBlockId = $state<string | null>(null);

	const current = $derived(activeLesson());
	const visibleBlocks = $derived(sectionBlocks(library.activeSectionId));
	const firstBlockId = $derived(visibleBlocks[0]?.id ?? null);
	const activePlanItem = $derived(
		lesson.plan.find((item) => item.id === library.activeSectionId) ?? null
	);
	const recentActivity = $derived(lesson.activity.slice(-5).reverse());

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

	function writtenCount(record: { plan: { status?: string }[]; finished: boolean }) {
		if (!record.plan.length) return record.finished ? 1 : 0;
		return record.plan.filter((item) => item.status === 'written').length;
	}

	function cardStatus(record: {
		running: boolean;
		finished: boolean;
		phase: string;
		errorMessage: string;
		plan: { status?: string }[];
	}) {
		if (record.errorMessage) return 'stopped';
		if (record.running) return record.phase || 'researching';
		if (record.finished) return 'ready';
		if (record.plan.some((item) => item.status === 'written')) return 'in progress';
		return 'draft';
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
				audited for what is missing, and written to be listened to — one chapter at a time.
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
			<ul class="cards">
				{#each library.lessons as record (record.id)}
					<li>
						<button class="card" onclick={() => openLesson(record.id)}>
							<h2>{record.topic}</h2>
							<p class="cardmeta">
								<span class="status">{cardStatus(record)}</span>
								{#if record.plan.length}
									<span>{writtenCount(record)} of {record.plan.length} sections</span>
								{/if}
							</p>
							{#if record.id === library.activeId && record.running}
								<ul class="cardactivity">
									{#each recentActivity.slice(0, 4) as entry, entryIndex (entryIndex)}
										<li>{entry.text}</li>
									{/each}
								</ul>
							{:else if record.errorMessage}
								<p class="carderror">{record.errorMessage}</p>
							{:else if record.plan.length}
								<ol class="cardplan">
									{#each record.plan.slice(0, 4) as item (item.id)}
										<li class:done={item.status === 'written'}>{item.title}</li>
									{/each}
								</ol>
							{/if}
						</button>
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
{:else if library.view === 'topics'}
	<div class="topics">
		<button class="back" onclick={backToLibrary}>← Library</button>
		<h1 class="topic">{lesson.topic}</h1>
		{#if lesson.running}
			<p class="lede">{lesson.phase || 'working'} · progress lives on the library card</p>
		{/if}
		{#if lesson.errorMessage}
			<p class="railerror">{lesson.errorMessage}</p>
		{/if}
		{#if lesson.plan.length}
			<ol class="topiclist">
				{#each lesson.plan as item (item.id)}
					<li>
						<button class="topicrow" onclick={() => openSection(item.id)}>
							<span class="tmark" class:done={item.status === 'written'}></span>
							<span class="tbody">
								<span class="ttitle">{item.title}</span>
								{#if item.angle}<span class="tangle">{item.angle}</span>{/if}
							</span>
							<span class="tstat">{item.status === 'written' ? 'ready' : 'pending'}</span>
						</button>
					</li>
				{/each}
			</ol>
		{:else}
			<p class="lede">
				{lesson.running
					? 'The plan will land here once the brainstorming pass finishes.'
					: 'No sections yet.'}
			</p>
		{/if}
	</div>
{:else}
	<div class="lesson" class:hasbar={visibleBlocks.length > 0}>
		<button class="back" onclick={current?.plan.length ? backToTopics : backToLibrary}>
			{current?.plan.length ? '← Sections' : '← Library'}
		</button>
		<h1 class="topic">{activePlanItem?.title || lesson.topic}</h1>
		{#if activePlanItem?.angle}
			<p class="lede">{activePlanItem.angle}</p>
		{/if}

		{#if visibleBlocks.length}
			<div class="transport">
				{#if playback.playing}
					<button class="ghost" onclick={stopPlayback}>◼ Stop</button>
				{:else if firstBlockId}
					<button class="ghost" onclick={() => playFrom(firstBlockId)}>▶ Play</button>
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
		{:else if lesson.running}
			<p class="lede">This section has not been written yet. It will appear here as soon as it is.</p>
		{/if}

		{#each visibleBlocks as block (block.id)}
			<BlockView
				{block}
				onRequestActions={(target) => (sheetBlock = target)}
				onFocused={(blockId) => (anchorBlockId = blockId)}
			/>
		{/each}

		{#if lesson.finished && visibleBlocks.length}
			<p class="footnote">
				Ask anything below, or put the cursor into any paragraph and type — your question becomes
				part of the episode, and the answer lands right under it. Enter starts a new paragraph.
			</p>
		{/if}
	</div>

	{#if visibleBlocks.length}
		<AskBar {anchorBlockId} />
	{/if}
	<BlockActions block={sheetBlock} onClose={() => (sheetBlock = null)} />
{/if}

<style>
	.library,
	.topics,
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
	.cards {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: 100%;
		text-align: left;
		padding: 16px 16px 14px;
		border: 1.5px solid var(--line);
		border-radius: 16px;
		background: var(--panel);
	}
	.card h2 {
		font-family: var(--serif);
		font-size: 18px;
		line-height: 1.35;
		margin: 0;
		font-weight: 600;
	}
	.cardmeta {
		display: flex;
		gap: 10px;
		margin: 0;
		font-size: 12px;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}
	.cardactivity,
	.cardplan {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.cardactivity li,
	.cardplan li,
	.carderror {
		font-size: 13px;
		color: var(--muted);
		line-height: 1.4;
	}
	.cardplan {
		list-style: decimal;
		padding-left: 18px;
	}
	.cardplan li.done {
		color: var(--ok);
	}
	.carderror {
		color: var(--bad);
		margin: 0;
	}

	.back {
		font-size: 13px;
		color: var(--muted);
		margin: 0 0 12px;
		padding: 0;
	}
	.topiclist {
		list-style: none;
		margin: 18px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.topicrow {
		display: flex;
		gap: 12px;
		align-items: flex-start;
		width: 100%;
		text-align: left;
		padding: 14px 14px;
		border: 1.5px solid var(--line);
		border-radius: 14px;
		background: var(--panel);
	}
	.tmark {
		width: 9px;
		height: 9px;
		margin-top: 6px;
		border-radius: 50%;
		border: 1.5px solid var(--line);
		flex: 0 0 9px;
	}
	.tmark.done {
		background: var(--ok);
		border-color: var(--ok);
	}
	.tbody {
		display: flex;
		flex-direction: column;
		gap: 4px;
		flex: 1;
		min-width: 0;
	}
	.ttitle {
		font-size: 16px;
		line-height: 1.35;
	}
	.tangle {
		font-size: 13px;
		color: var(--muted);
		line-height: 1.45;
	}
	.tstat {
		font-size: 11px;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		margin-top: 4px;
	}
	.railerror {
		margin: 8px 0 0;
		font-size: 13px;
		color: var(--bad);
		line-height: 1.5;
	}

	.transport {
		display: flex;
		align-items: center;
		gap: 10px;
		margin: 18px 0 22px;
		flex-wrap: wrap;
	}
	.ghost {
		padding: 9px 15px;
		border-radius: 999px;
		border: 1.5px solid var(--line);
		font-size: 14px;
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
		border-top: 1px solid var(--line);
		padding-top: 14px;
	}
</style>
