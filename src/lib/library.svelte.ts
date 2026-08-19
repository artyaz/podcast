import {
	lesson,
	onLessonChange,
	type Block,
	type PlanItem,
	type ResearchActivity
} from './lesson.svelte';
import { putPlain, readPlain } from './vault';

export interface LessonRecord {
	id: string;
	topic: string;
	createdAt: number;
	updatedAt: number;
	subtopics: { title: string; angle?: string }[];
	plan: PlanItem[];
	blocks: Block[];
	researchState: Record<string, unknown> | null;
	running: boolean;
	finished: boolean;
	phase: string;
	slicesUsed: number;
	activity: ResearchActivity[];
	errorMessage: string;
}

export type LibraryView = 'library' | 'topics' | 'lesson';

interface LibraryState {
	lessons: LessonRecord[];
	activeId: string | null;
	activeSectionId: string | null;
	view: LibraryView;
}

export const library = $state<LibraryState>({
	lessons: [],
	activeId: null,
	activeSectionId: null,
	view: 'library'
});

function newLessonId(): string {
	return `les_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function snapshotLesson(recordId: string): LessonRecord | null {
	return library.lessons.find((entry) => entry.id === recordId) ?? null;
}

export function activeLesson(): LessonRecord | null {
	if (!library.activeId) return null;
	return snapshotLesson(library.activeId);
}

function writeLesson(record: LessonRecord) {
	const index = library.lessons.findIndex((entry) => entry.id === record.id);
	record.updatedAt = Date.now();
	if (index >= 0) library.lessons[index] = record;
	else library.lessons.unshift(record);
	void putPlain(`lesson:${record.id}`, record);
	void putPlain(
		'index',
		library.lessons.map((entry) => entry.id)
	);
}

export function hydrateLibrary(rows: Record<string, unknown>) {
	const ids = (rows.index as string[]) || [];
	const loaded: LessonRecord[] = [];
	const seen = new Set<string>();
	for (const id of ids) {
		const record = rows[`lesson:${id}`] as LessonRecord | undefined;
		if (record?.id) {
			loaded.push(record);
			seen.add(record.id);
		}
	}
	for (const [rowId, value] of Object.entries(rows)) {
		if (!rowId.startsWith('lesson:')) continue;
		const record = value as LessonRecord;
		if (record?.id && !seen.has(record.id)) loaded.push(record);
	}
	loaded.sort((left, right) => (right.updatedAt || 0) - (left.updatedAt || 0));
	for (const record of loaded) record.running = false;
	library.lessons = loaded;
}

export function persistActiveFromWorkingDocument() {
	if (!library.activeId) return;
	const existing = snapshotLesson(library.activeId);
	if (!existing) return;
	writeLesson({
		...existing,
		topic: lesson.topic || existing.topic,
		subtopics: lesson.subtopics,
		plan: lesson.plan || existing.plan,
		blocks: lesson.blocks,
		researchState: lesson.researchState,
		running: lesson.running,
		finished: lesson.finished,
		phase: lesson.phase,
		slicesUsed: lesson.slicesUsed,
		activity: lesson.activity,
		errorMessage: lesson.errorMessage
	});
}

export function loadWorkingDocument(record: LessonRecord) {
	lesson.topic = record.topic;
	lesson.subtopics = record.subtopics || [];
	lesson.plan = record.plan || [];
	lesson.blocks = record.blocks || [];
	lesson.researchState = record.researchState;
	lesson.running = record.running;
	lesson.finished = record.finished;
	lesson.phase = record.phase;
	lesson.slicesUsed = record.slicesUsed;
	lesson.activity = record.activity || [];
	lesson.errorMessage = record.errorMessage || '';
}

export function createLesson(topic: string): LessonRecord {
	const record: LessonRecord = {
		id: newLessonId(),
		topic,
		createdAt: Date.now(),
		updatedAt: Date.now(),
		subtopics: [],
		plan: [],
		blocks: [],
		researchState: null,
		running: false,
		finished: false,
		phase: '',
		slicesUsed: 0,
		activity: [],
		errorMessage: ''
	};
	library.lessons.unshift(record);
	library.activeId = record.id;
	library.activeSectionId = null;
	library.view = 'library';
	void putPlain(`lesson:${record.id}`, record);
	void putPlain(
		'index',
		library.lessons.map((entry) => entry.id)
	);
	return record;
}

export function openLesson(recordId: string) {
	const record = snapshotLesson(recordId);
	if (!record) return;
	library.activeId = recordId;
	loadWorkingDocument(record);
	if ((record.plan || []).length) {
		library.view = 'topics';
		library.activeSectionId = null;
	} else if (record.blocks.length) {
		library.view = 'lesson';
		library.activeSectionId = null;
	} else {
		library.view = 'topics';
	}
}

export function openSection(sectionId: string) {
	library.activeSectionId = sectionId;
	library.view = 'lesson';
}

export function backToLibrary() {
	persistActiveFromWorkingDocument();
	library.view = 'library';
	library.activeSectionId = null;
}

export function backToTopics() {
	library.view = 'topics';
	library.activeSectionId = null;
}

export function sectionBlocks(sectionId: string | null): Block[] {
	if (!sectionId) return lesson.blocks;
	return lesson.blocks.filter((block) => block.sectionId === sectionId);
}

/** Used so the first unlock can restore a lesson that was only in memory. */
export function peekStoredIndex(): string[] {
	return (readPlain<string[]>('index') as string[]) || [];
}

onLessonChange(persistActiveFromWorkingDocument);
