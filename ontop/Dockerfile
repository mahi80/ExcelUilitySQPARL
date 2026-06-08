FROM ontop/ontop:5.1.0

# Ontop's base image ships only the H2 driver; add the PostgreSQL JDBC driver.
USER root
COPY postgresql-*.jar /opt/ontop/jdbc/
RUN chmod 755 /opt/ontop/jdbc && chmod 644 /opt/ontop/jdbc/postgresql-*.jar
USER ontop
