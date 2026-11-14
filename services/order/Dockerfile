# Build stage runs unit tests (mvn package without -DskipTests)
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY pom.xml .
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -B package

FROM eclipse-temurin:21-jre
RUN useradd -r -u 10001 app
WORKDIR /app
COPY --from=build /src/target/app.jar /app/app.jar
USER 10001
EXPOSE 8080
# Build-time image defect: restart preserves it; rolling back the image removes it.
ARG RELEASE_FAULTS='{"faults":{}}'
ENV RELEASE_FAULTS=${RELEASE_FAULTS}
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError"
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
