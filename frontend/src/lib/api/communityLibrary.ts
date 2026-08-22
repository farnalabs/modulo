export interface CommunityLibraryEntry {
  id: string;
  type: string;
  slug: string;
  author?: string | null;
  version?: string | null;
  license?: string | null;
  status?: string | null;
  published_at?: string | null;
  content_sha256?: string | null;
  installed?: boolean;
  content?: unknown;
}
