// Reference: https://github.com/Microsoft/TypeScript-React-Starter/issues/12#issuecomment-327860151
// TS compatibility for https://github.com/webpack-contrib/file-loader

declare module "*.ne" {
  const _: any;
  export default _;
}
