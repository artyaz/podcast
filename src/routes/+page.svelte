<script lang="ts">
	import AskBar from '$lib/components/AskBar.svelte';
	import BlockActions from '$lib/components/BlockActions.svelte';
	import BlockView from '$lib/components/BlockView.svelte';
	import SubtopicModal from '$lib/components/SubtopicModal.svelte';
	import { playFrom, playback, stopPlayback } from '$lib/audio.svelte';
	import { lesson, runResearch, type Block } from '$lib/lesson.svelte';
	import { canResearch, settings } from '$lib/settings.svelte';

	let topicDraft = $state('');
	let showSubtopicModal = $state(false);
	let sheetBlock = $state<Block | null>(null);
	let anchorBlockId = $state<string | null>(null);
	let showActivity = $state(true);

	const recentActivity = $derived(lesson.activity.slice(-7).reverse());
	const firstBlockId = $derived(lesson.blocks[0]?.id ?? null);

	function begin() {
		const topic = topicDraft.trim();
		if (!topic) return;
		runResearch(topic);
	}

	function beginWithSegments(subtopics: { title: string; angle?: string }[]) {
		const topic = topicDraft.trim();
		showSubtopicModal = false;
		if (!topic || !subtopics.length) return;
		runResearch(topic, subtopics);
	}
</script>

<svelte:head><title>{lesson.topic || 'Praxis'}</title></svelte:head>

{#if !lesson.topic}
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

	{#if showSubtopicModal}
		<SubtopicModal
			topic={topicDraft.trim()}
			onClose={() => (showSubtopicModal = false)}
			onProceed={beginWithSegments}
		/>
	{/if}
{:else}
	<div class="lesson" class:hasbar={lesson.blocks.length > 0}>
		<h1 class="topic">{lesson.topic}</h1>

		{#if lesson.subtopics.length}
			<ol class="spine">
				{#each lesson.subtopics as segment, segmentIndex (segment.title + segmentIndex)}
					<li>{segment.title}</li>
				{/each}
			</ol>
		{/if}

		{#if lesson.running || lesson.errorMessage}
			<div class="rail">
				<div class="railhead">
					<span class="phaselabel">
						{#if lesson.running}
							<span class="pulse"></span>
							{lesson.phase || 'starting'}
							{#if lesson.slicesUsed > 1}· pass {lesson.slicesUsed}{/if}
						{:else}
							stopped
						{/if}
					</span>
					<button class="ghost tiny" onclick={() => (showActivity = !showActivity)}>
						{showActivity ? 'hide' : 'show'}
					</button>
				</div>

				{#if lesson.errorMessage}
					<p class="railerror">{lesson.errorMessage}</p>
				{/if}

				{#if showActivity}
					<ul class="activity">
						{#each recentActivity as entry, entryIndex (entryIndex)}
							<li class={entry.kind}>
								<span class="what">{entry.text}</span>
								{#if entry.detail}<span class="detail">{entry.detail}</span>{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</div>
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

		{#if lesson.finished}
			<p class="footnote">
				Ask anything below, or put the cursor into any paragraph and type — your question becomes
				part of the episode, and the answer lands right under it.
			</p>
		{/if}
	</div>

	{#if lesson.blocks.length}
		<AskBar {anchorBlockId} />
	{/if}
	<BlockActions block={sheetBlock} onClose={() => (sheetBlock = null)} />
{/if}

<style>
	.opening {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 12px 0 40px;
	}
	h1 {
		font-family: var(--serif);
		font-size: 28px;
		line-height: 1.25;
		margin: 0;
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

	.lesson {
		padding-bottom: 40px;
	}
	.lesson.hasbar {
		padding-bottom: 96px;
	}
	.topic {
		font-size: 25px;
		margin: 0 0 20px;
	}

	.spine {
		list-style: decimal;
		margin: -8px 0 22px;
		padding-left: 20px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.spine li {
		font-size: 13px;
		color: var(--muted);
		line-height: 1.45;
	}
	.rail {
		border: 1px solid var(--line);
		border-radius: 12px;
		padding: 11px 13px;
		margin-bottom: 26px;
		background: var(--panel);
	}
	.railhead {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.phaselabel {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.07em;
		color: var(--muted);
	}
	.pulse {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--ok);
		animation: breathe 1.4s ease-in-out infinite;
	}
	@keyframes breathe {
		0%,
		100% {
			opacity: 0.3;
		}
		50% {
			opacity: 1;
		}
	}
	.railerror {
		margin: 8px 0 0;
		font-size: 13px;
		color: var(--bad);
		line-height: 1.5;
	}
	.activity {
		list-style: none;
		margin: 9px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.activity li {
		font-size: 12.5px;
		line-height: 1.45;
		color: var(--muted);
		display: flex;
		flex-direction: column;
	}
	.activity li.search .what::before {
		content: '⌕ ';
	}
	.activity li.read .what::before {
		content: '▤ ';
	}
	.activity li.gap .what {
		color: var(--warn);
	}
	.activity li.error .what {
		color: var(--bad);
	}
	.activity .detail {
		font-size: 11.5px;
		opacity: 0.72;
		white-space: pre-wrap;
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
		border-top: 1px solid var(--line);
		padding-top: 14px;
	}
</style>
