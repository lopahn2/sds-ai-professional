FROM eclipse-temurin@sha256:49e21e16e3c86eb7816a44a67549910ed090fbeb40c29c525d58bf5e02e91b0f
COPY target/reservation-1.0.0.jar /app.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-XX:ActiveProcessorCount=2", "-jar", "/app.jar"]
