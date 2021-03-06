// NOTE: Make sure to update the search.ne grammar with additional filters
// should they be added here.

export enum Filter {
  Id = 'id',
  Title = 'title',
  Artist = 'artist',
  Album = 'album',
  Release = 'release',
  Publisher = 'publisher',
  Genre = 'genre',
  Key = 'key',
  Artwork = 'artwork',
}

type SearchValue = {
  value: string;
  offset: number;
};

export type SearchToken =
  | {
      type: 'filter';
      offset: number;
      key: {type: Filter; offset: number};
      value: SearchValue;
    }
  | ({type: 'freeText' | 'blankSpace'} & SearchValue);
