declare module "*.css?raw" {
  const content: string;
  export default content;
}

declare module "*.png" {
  const src: string;
  export default src;
}

declare module "*.gif" {
  const src: string;
  export default src;
}

interface ImportMeta {
  glob: (
    pattern: string,
    options?: Record<string, unknown>,
  ) => Record<string, unknown>;
}
