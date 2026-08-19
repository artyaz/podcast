import { absorbUsage, backendUrl, llmPayload, secretsPayload, settings } from './settings.svelte';

/**
 * The lesson document and the two loops that fill it.
 *
 * A lesson is a flat, ordered array of blocks — closer to a list of spoken beats
 * than to a rich-text document, which is why this is a small store rather than a
 * borrowed editor. Each block is independently playable and independently
 * editable, and a question can be dropped between any two of them.
 *
 * The research loop is the interesting part. The backend cannot finish deep
 * research inside one Vercel invocation, so it suspends and returns its state.
 * `runResearch` posts that state straight back and keeps going until the lesson
 * exists. From the reader's side it looks like one long operation.
 */

export type BlockKind = 'heading' | 'paragraph' | 'aside' | 'gap';
export type ClaimStatus = 'verified' | 'contested' | 'unverified' | 'inferred';
export type BlockOrigin = 'lesson' | 'answer' | 'question' | 'user';

export interface SourceReference {
	url: string;
	title?: string;
}

export interface PlanItem {
	id: string;
	title: string;
	angle?: string;
	status?: 'pending' | 'written';
}

export interface Block {
	id: string;
	kind: BlockKind;
	text: string;
	sources: SourceReference[];
	status: ClaimStatus;
	origin: BlockOrigin;
	/** True while the model is still working on this block's content. */
	pending?: boolean;
	/** Plan section this block belongs to, when the episode is written in chapters. */
	sectionId?: string;
}

export interface ResearchActivity {
	kind: 'search' | 'read' | 'phase' | 'gap' | 'note' | 'error';
	text: string;
	detail?: string;
}

export interface WorkBeat {
	title: string;
	reasoning: string;
}

interface LessonState {
	topic: string;
	/** The chosen episode spine, empty when the topic was researched freely. */
	subtopics: { title: string; angle?: string }[];
	/** Chapter list the writer walks, one section at a time. */
	plan: PlanItem[];
	blocks: Block[];
	/** The backend's research checkpoint. Kept so an inline ask can reuse findings. */
	researchState: Record<string, unknown> | null;
	running: boolean;
	finished: boolean;
	phase: string;
	slicesUsed: number;
	activity: ResearchActivity[];
	work: WorkBeat[];
	errorMessage: string;
}

export const lesson = $state<LessonState>({
	topic: '',
	subtopics: [],
	plan: [],
	blocks: [],
	researchState: null,
	running: false,
	finished: false,
	phase: '',
	slicesUsed: 0,
	activity: [],
	work: [],
	errorMessage: ''
});

let persistHook: (() => void) | null = null;

/** Library (and the vault) register here so every mutation hits IndexedDB. */
export function onLessonChange(hook: () => void) {
	persistHook = hook;
}

function touch() {
	persistHook?.();
}

const MAXIMUM_SLICES = 14;

function newBlockId(): string {
	return `blk_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

function note(entry: ResearchActivity) {
	lesson.activity.push(entry);
	if (lesson.activity.length > 200) lesson.activity.splice(0, lesson.activity.length - 200);
	touch();
}

/**
 * Read a server-sent event stream from a POST response.
 *
 * EventSource cannot issue a POST and these requests carry a body of keys and
 * state, so the framing is parsed by hand. Chunks split mid-event, so anything
 * after the last blank-line separator is held back for the next read.
 */
async function* readEventStream(response: Response): AsyncGenerator<Record<string, unknown>> {
	if (!response.body) throw new Error('the backend returned no stream');
	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	let buffered = '';

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		buffered += decoder.decode(value, { stream: true });

		const events = buffered.split('\n\n');
		buffered = events.pop() ?? '';

		for (const rawEvent of events) {
			const dataLines = rawEvent
				.split('\n')
				.filter((line) => line.startsWith('data:'))
				.map((line) => line.slice(5).trim());
			if (!dataLines.length) continue;
			try {
				yield JSON.parse(dataLines.join('')) as Record<string, unknown>;
			} catch {
				// A malformed frame is not worth killing the run over.
			}
		}
	}
}

async function postForStream(path: string, body: unknown): Promise<Response> {
	const response = await fetch(backendUrl(path), {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!response.ok) {
		let detail = `HTTP ${response.status}`;
		try {
			const parsed = await response.json();
			detail = parsed.detail || detail;
		} catch {
			/* keep the status line */
		}
		throw new Error(detail);
	}
	return response;
}

/** Record whatever a research event tells the reader, for the progress rail. */
function absorbResearchEvent(event: Record<string, unknown>): void {
	const eventType = event.type as string;

	if (eventType === 'phase') {
		lesson.phase = (event.phase as string) || '';
		note({ kind: 'phase', text: (event.message as string) || lesson.phase });
	} else if (eventType === 'scoped') {
		const openQuestions = (event.open_questions as string[]) || [];
		note({
			kind: 'phase',
			text: `Scoped: ${(event.governing_axis as string) || 'question split apart'}`,
			detail: openQuestions.join('\n')
		});
	} else if (eventType === 'search') {
		note({
			kind: 'search',
			text: event.query as string,
			detail: `mode: ${event.mode as string}`
		});
	} else if (eventType === 'read') {
		note({ kind: 'read', text: event.url as string });
	} else if (eventType === 'findings') {
		const byStatus = (event.by_status as Record<string, number>) || {};
		const tally = Object.entries(byStatus)
			.map(([status, count]) => `${count} ${status}`)
			.join(', ');
		note({ kind: 'note', text: `${event.total} findings (${tally})` });
	} else if (eventType === 'gap_check') {
		const forced = event.forced_another_round as string;
		note({
			kind: 'gap',
			text: event.ready
				? 'Enough evidence to write'
				: `Another round needed${forced ? ` — ${forced}` : ''}`,
			detail: (event.reasoning as string) || ''
		});
	} else if (eventType === 'suspend') {
		note({ kind: 'note', text: `Paused at ${event.phase} — resuming` });
	} else if (eventType === 'tool_error') {
		note({ kind: 'error', text: event.error as string });
	} else if (eventType === 'note') {
		note({ kind: 'note', text: event.message as string });
	} else if (eventType === 'plan') {
		lesson.plan = ((event.plan as PlanItem[]) || []).map((item) => ({ ...item }));
		note({
			kind: 'phase',
			text: `Plan: ${lesson.plan.length} sections`,
			detail: lesson.plan.map((item) => item.title).join('\n')
		});
	} else if (eventType === 'brainstorm') {
		const tensions = (event.tensions as string[]) || [];
		note({
			kind: 'phase',
			text: 'Brainstorm',
			detail: tensions.slice(0, 4).join('\n')
		});
	} else if (eventType === 'work') {
		const title = ((event.title as string) || '').trim();
		const reasoning = ((event.reasoning as string) || '').trim();
		if (title || reasoning) {
			lesson.work.push({ title: title || 'Working', reasoning });
			touch();
		}
	} else if (eventType === 'blocks_delta') {
		const incoming = ((event.blocks as Block[]) || []).map((block) => ({
			...block,
			pending: false
		}));
		lesson.blocks = [...lesson.blocks, ...incoming];
		note({
			kind: 'note',
			text: `Wrote ${(event.count as number) || incoming.length} blocks (${event.written}/${event.total})`
		});
	}
}

/**
 * Research a topic into a lesson, resuming across as many invocations as it takes.
 */
export async function runResearch(
	topic: string,
	subtopics: { title: string; angle?: string }[] = []
): Promise<void> {
	lesson.topic = topic;
	lesson.subtopics = subtopics;
	lesson.plan = [];
	lesson.blocks = [];
	lesson.researchState = null;
	lesson.running = true;
	lesson.finished = false;
	lesson.errorMessage = '';
	lesson.activity = [];
	lesson.work = [];
	lesson.slicesUsed = 0;
	touch();

	let carriedState: Record<string, unknown> | null = null;

	try {
		while (lesson.slicesUsed < MAXIMUM_SLICES) {
			lesson.slicesUsed += 1;

			const response = await postForStream('/api/research', {
				topic: carriedState ? undefined : topic,
				state: carriedState ?? undefined,
				secrets: secretsPayload(),
				llm: llmPayload(),
				// Only sent on the opening request; afterwards the spine lives in the
				// checkpoint and resending it would fight the carried state.
				subtopics: carriedState ? undefined : subtopics,
				budget_seconds: settings.budgetSeconds,
				minimum_rounds: settings.minimumRounds,
				maximum_rounds: settings.maximumRounds,
				target_block_count: settings.targetBlockCount
			});

			let sliceOutcome = '';
			for await (const event of readEventStream(response)) {
				const eventType = event.type as string;

				if (eventType === 'error') {
					throw new Error((event.error as string) || 'the backend failed');
				}
				if (eventType === 'blocks') {
					lesson.blocks = ((event.blocks as Block[]) || []).map((block) => ({
						...block,
						pending: false
					}));
				}
				if (eventType === 'done' || eventType === 'suspended') {
					sliceOutcome = eventType;
					carriedState = (event.state as Record<string, unknown>) || null;
					lesson.researchState = carriedState;
					absorbUsage(event.usage);
					break;
				}
				absorbResearchEvent(event);
			}

			if (sliceOutcome === 'done' || sliceOutcome === 'suspended') {
				const checkpoint = carriedState || {};
				if (Array.isArray(checkpoint.plan)) {
					lesson.plan = checkpoint.plan as PlanItem[];
				}
				if (Array.isArray(checkpoint.blocks) && checkpoint.blocks.length >= lesson.blocks.length) {
					lesson.blocks = (checkpoint.blocks as Block[]).map((block) => ({
						...block,
						pending: false
					}));
				}
				touch();
			}

			if (sliceOutcome === 'done') {
				lesson.finished = true;
				lesson.phase = 'done';
				touch();
				return;
			}
			if (sliceOutcome !== 'suspended') {
				if (carriedState) {
					note({
						kind: 'note',
						text: 'stream dropped — retrying from last checkpoint'
					});
					continue;
				}
				throw new Error('the stream ended without finishing or suspending');
			}
		}
		throw new Error(`gave up after ${MAXIMUM_SLICES} slices without finishing`);
	} catch (failure) {
		lesson.errorMessage = (failure as Error).message;
		note({ kind: 'error', text: lesson.errorMessage });
	} finally {
		lesson.running = false;
		touch();
	}
}

export function indexOfBlock(blockId: string): number {
	return lesson.blocks.findIndex((block) => block.id === blockId);
}

/** Roughly what a listener heard just before this point, to orient the answer. */
function contextAround(insertAtIndex: number): string {
	const start = Math.max(0, insertAtIndex - 2);
	return lesson.blocks
		.slice(start, insertAtIndex)
		.map((block) => block.text)
		.join('\n\n');
}

/**
 * Ask a question at a point in the lesson.
 *
 * The question becomes a block in the lesson, because it is part of the episode
 * now — it gets read aloud in sequence like anything else. A placeholder answer
 * block shimmers directly beneath it until the real blocks arrive.
 */
export async function askAt(afterBlockId: string | null, questionText: string): Promise<void> {
	const trimmedQuestion = questionText.trim();
	if (!trimmedQuestion) return;

	const anchorIndex = afterBlockId ? indexOfBlock(afterBlockId) : lesson.blocks.length - 1;
	const insertAtIndex = anchorIndex < 0 ? lesson.blocks.length : anchorIndex + 1;

	const questionBlock: Block = {
		id: newBlockId(),
		kind: 'paragraph',
		text: trimmedQuestion,
		sources: [],
		status: 'inferred',
		origin: 'question'
	};
	const placeholderBlock: Block = {
		id: newBlockId(),
		kind: 'paragraph',
		text: 'Looking into that…',
		sources: [],
		status: 'unverified',
		origin: 'answer',
		pending: true
	};

	lesson.blocks.splice(insertAtIndex, 0, questionBlock, placeholderBlock);
	touch();

	try {
		const response = await postForStream('/api/ask', {
			topic: lesson.topic,
			question: trimmedQuestion,
			context: contextAround(insertAtIndex),
			findings: (lesson.researchState?.findings as unknown[]) || [],
			secrets: secretsPayload(),
			llm: llmPayload(),
			budget_seconds: Math.min(settings.budgetSeconds, 150)
		});

		for await (const event of readEventStream(response)) {
			const eventType = event.type as string;

			if (eventType === 'error') {
				throw new Error((event.error as string) || 'the backend failed');
			}
			if (eventType === 'search') {
				placeholderBlock.text = `Searching: ${event.query as string}`;
			} else if (eventType === 'read') {
				placeholderBlock.text = `Reading ${event.url as string}`;
			} else if (eventType === 'done') {
				absorbUsage(event.usage);
				const answerBlocks = ((event.blocks as Block[]) || []).map((block) => ({
					...block,
					pending: false
				}));
				const placeholderIndex = indexOfBlock(placeholderBlock.id);
				if (placeholderIndex >= 0) {
					lesson.blocks.splice(
						placeholderIndex,
						1,
						...(answerBlocks.length
							? answerBlocks
							: [
									{
										...placeholderBlock,
										pending: false,
										text: 'That could not be established from the sources available.',
										status: 'unverified' as ClaimStatus
									}
								])
					);
				}
				return;
			}
		}
	} catch (failure) {
		const placeholderIndex = indexOfBlock(placeholderBlock.id);
		if (placeholderIndex >= 0) {
			lesson.blocks[placeholderIndex] = {
				...placeholderBlock,
				pending: false,
				text: `That question failed: ${(failure as Error).message}`,
				status: 'unverified'
			};
		}
	}
}

export function updateBlockText(blockId: string, nextText: string): void {
	const blockIndex = indexOfBlock(blockId);
	if (blockIndex < 0) return;
	lesson.blocks[blockIndex].text = nextText;
	// A block the reader rewrote is no longer the model's verified claim.
	if (lesson.blocks[blockIndex].origin === 'lesson') {
		lesson.blocks[blockIndex].origin = 'user';
	}
	touch();
}

export function removeBlock(blockId: string): void {
	const blockIndex = indexOfBlock(blockId);
	if (blockIndex >= 0) lesson.blocks.splice(blockIndex, 1);
	touch();
}

export function splitBlockAt(blockId: string, offset: number): string | null {
	const blockIndex = indexOfBlock(blockId);
	if (blockIndex < 0) return null;
	const block = lesson.blocks[blockIndex];
	const safeOffset = Math.max(0, Math.min(offset, block.text.length));
	const before = block.text.slice(0, safeOffset);
	const after = block.text.slice(safeOffset);
	block.text = before;
	if (block.origin === 'lesson') block.origin = 'user';
	const created: Block = {
		id: newBlockId(),
		kind: 'paragraph',
		text: after,
		sources: [],
		status: 'unverified',
		origin: 'user',
		sectionId: block.sectionId
	};
	lesson.blocks.splice(blockIndex + 1, 0, created);
	touch();
	return created.id;
}

export function mergeBlockWithPrevious(blockId: string): string | null {
	const blockIndex = indexOfBlock(blockId);
	if (blockIndex <= 0) return null;
	const current = lesson.blocks[blockIndex];
	const previous = lesson.blocks[blockIndex - 1];
	previous.text = `${previous.text}${current.text}`;
	if (previous.origin === 'lesson') previous.origin = 'user';
	lesson.blocks.splice(blockIndex, 1);
	touch();
	return previous.id;
}

export function lessonAsPlainText(): string {
	return lesson.blocks.map((block) => block.text).join('\n\n');
}
