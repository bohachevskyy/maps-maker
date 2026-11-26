import { defineRailway, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "maps-maker";
export default defineRailway(() => {
  const mapsMaker = service("maps-maker", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/healthz",
    healthcheckTimeout: 60,
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 5 },
    replicas: { "europe-west4-drams3a": 1 },
  });

  return project("maps-maker", {
    variables: { managed: false },
    resources: [mapsMaker],
  });
});
