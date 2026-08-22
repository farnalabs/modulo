export interface CommunityLibraryEntryBase {
  id: string;
  type: string;
  slug: string;
  author?: string | null;
  version?: string | null;
  license?: string | null;
  status?: string | null;
  published_at?: string | null;
  content_sha256?: string | null;
}

export interface CommunityLibraryEntry extends CommunityLibraryEntryBase {
  installed: boolean;
}

export interface CommunityLibraryEntryDetail extends CommunityLibraryEntryBase {
  content?: unknown | null;
}
